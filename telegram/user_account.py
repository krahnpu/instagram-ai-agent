from pathlib import Path
from typing import Any, Dict, List, Optional
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError
from config import settings
from memory.long_term import LongTermMemory
from content.media_processor import MediaProcessor
from utils.security import SecretProtector
from utils.logger import get_logger


logger = get_logger("telegram.user_account")


class TelegramUserAccountConnector:
    """
    Interactive Telethon User Account Connector.
    Allows the Owner to connect a personal Telegram account step-by-step directly inside the bot:
    1. Receive & store encrypted API_ID and API_HASH
    2. Receive phone number & send login code (`send_code_request`)
    3. Receive verification code (`sign_in`)
    4. Handle Two-Step Verification password (`SessionPasswordNeededError`) if enabled
    5. Persist session and discover accessible channels for content monitoring
    """

    def __init__(self, lt_memory: LongTermMemory, media_processor: MediaProcessor) -> None:
        self.lt_memory = lt_memory
        self.media_processor = media_processor
        self.protector = SecretProtector(settings.SESSION_ENCRYPTION_KEY)
        self.client: Optional[TelegramClient] = None
        self.is_connected: bool = False
        self._pending_phone: Optional[str] = None
        self._pending_phone_code_hash: Optional[str] = None
        # True after submit_verification_code reports two_factor_required:
        # the next digits-only message from the owner is the 2FA password,
        # NOT a new phone number or login code.
        self._awaiting_2fa: bool = False
        # Called after a successful Telethon login (set by the orchestrator):
        # used to send the .session file to the owner as a backup.
        self.on_connected: Optional[Any] = None

    async def start(self) -> None:
        creds = await self.get_saved_credentials()
        api_id = creds["api_id"]
        api_hash = creds["api_hash"]
        # Restore a pending login interrupted by a process restart / spin-down:
        # the phone, code hash and 2FA flag live in SQLite (disk survives a
        # restart within the same deploy; RAM does not).
        self._pending_phone = await self.lt_memory.get_preference("tg_pending_phone", "") or None
        saved_hash_enc = await self.lt_memory.get_preference("tg_pending_phone_code_hash", "")
        self._pending_phone_code_hash = (
            self.protector.decrypt_if_needed(saved_hash_enc.encode("utf-8")) if saved_hash_enc else None
        )
        self._awaiting_2fa = (await self.lt_memory.get_preference("tg_awaiting_2fa", "")) == "1"
        if not api_id or not api_hash:
            return
        try:
            session_file = Path(settings.TG_SESSION_PATH)
            session_file.parent.mkdir(parents=True, exist_ok=True)
            self.client = TelegramClient(str(session_file), api_id, api_hash)
            await self.client.connect()
            self.is_connected = await self.client.is_user_authorized()
            if self.is_connected:
                await self._clear_pending_login()
            logger.info(f"Telethon user client initialized (authorized={self.is_connected})")
        except Exception as exc:
            logger.error(f"Telethon initialization error: {exc}")
            self.is_connected = False

    async def _save_pending_login(self) -> None:
        await self.lt_memory.set_preference("tg_pending_phone", self._pending_phone or "")
        enc_hash = (
            self.protector.encrypt_if_enabled(self._pending_phone_code_hash or "").decode("utf-8")
            if self._pending_phone_code_hash
            else ""
        )
        await self.lt_memory.set_preference("tg_pending_phone_code_hash", enc_hash)
        await self.lt_memory.set_preference("tg_awaiting_2fa", "1" if self._awaiting_2fa else "")

    async def _clear_pending_login(self) -> None:
        self._pending_phone = None
        self._pending_phone_code_hash = None
        self._awaiting_2fa = False
        await self.lt_memory.set_preference("tg_pending_phone", "")
        await self.lt_memory.set_preference("tg_pending_phone_code_hash", "")
        await self.lt_memory.set_preference("tg_awaiting_2fa", "")

    async def _notify_connected(self) -> None:
        if self.on_connected:
            try:
                await self.on_connected()
            except Exception as exc:
                logger.error(f"Telethon on_connected callback error: {exc}")

    async def get_saved_credentials(self) -> Dict[str, Any]:
        saved_id = await self.lt_memory.get_preference("tg_user_api_id", "")
        saved_hash_enc = await self.lt_memory.get_preference("tg_user_api_hash", "")
        api_id = int(saved_id) if saved_id and saved_id.isdigit() else settings.TG_API_ID
        api_hash = (
            self.protector.decrypt_if_needed(saved_hash_enc.encode("utf-8"))
            if saved_hash_enc
            else settings.TG_API_HASH
        )
        return {"api_id": api_id, "api_hash": api_hash}

    async def begin_interactive_login(self, api_id: int, api_hash: str, phone: str) -> Dict[str, Any]:
        """Step 1-3: Save API_ID/API_HASH, connect Telethon client, and request SMS/Telegram code."""
        self._awaiting_2fa = False
        await self.lt_memory.set_preference("tg_user_api_id", str(api_id))
        enc_hash = self.protector.encrypt_if_enabled(api_hash.strip()).decode("utf-8")
        await self.lt_memory.set_preference("tg_user_api_hash", enc_hash)

        try:
            session_file = Path(settings.TG_SESSION_PATH)
            session_file.parent.mkdir(parents=True, exist_ok=True)
            if self.client:
                await self.client.disconnect()
            self.client = TelegramClient(str(session_file), int(api_id), api_hash.strip())
            await self.client.connect()

            if await self.client.is_user_authorized():
                self.is_connected = True
                return {"status": "already_authorized", "message": "حساب تلگرام از قبل متصل و معتبر است."}

            sent = await self.client.send_code_request(phone.strip())
            self._pending_phone = phone.strip()
            self._pending_phone_code_hash = sent.phone_code_hash
            await self._save_pending_login()
            return {
                "status": "code_sent",
                "phone": self._pending_phone,
                "message": "کد تأیید تلگرام ارسال شد. لطفاً کد دریافتی را وارد کنید.",
            }
        except Exception as exc:
            logger.error(f"Telethon send_code_request error: {exc}")
            return {"status": "error", "error": str(exc)}

    async def submit_verification_code(self, code: str) -> Dict[str, Any]:
        """Step 4: Submit verification code; detects if 2FA password is required."""
        if not self.client or not self._pending_phone or not self._pending_phone_code_hash:
            return {"status": "error", "error": "نشست در انتظار کد تأیید یافت نشد. ابتدا شماره تلفن را ثبت کنید."}

        clean_code = code.strip().replace("-", "").replace(" ", "")
        try:
            await self.client.sign_in(
                phone=self._pending_phone,
                code=clean_code,
                phone_code_hash=self._pending_phone_code_hash,
            )
            self.is_connected = True
            self._awaiting_2fa = False
            await self._clear_pending_login()
            me = await self.client.get_me()
            await self._notify_connected()
            return {
                "status": "connected",
                "username": getattr(me, "username", "") or str( getattr(me, "id", "")),
                "message": "اتصال حساب کاربری تلگرام با موفقیت انجام و Session به صورت پایدار ذخیره شد.",
            }
        except SessionPasswordNeededError:
            self._awaiting_2fa = True
            await self._save_pending_login()
            return {
                "status": "two_factor_required",
                "message": "تأیید دومرحله‌ای (Two-Step Verification) روی این حساب فعال است. لطفاً رمز عبور دومرحله‌ای را ارسال کنید.",
            }
        except PhoneCodeInvalidError:
            return {"status": "invalid_code", "error": "کد تأیید واردشده نامعتبر است."}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    async def submit_two_factor_password(self, password: str) -> Dict[str, Any]:
        """Step 5: Complete Telethon sign-in using 2FA cloud password."""
        if not self.client:
            return {"status": "error", "error": "کلاینت Telethon فعال نیست."}
        try:
            await self.client.sign_in(password=password)
            self.is_connected = True
            self._awaiting_2fa = False
            await self._clear_pending_login()
            me = await self.client.get_me()
            await self._notify_connected()
            return {
                "status": "connected",
                "username": getattr(me, "username", "") or str(getattr(me, "id", "")),
                "message": "احراز هویت دومرحله‌ای با موفقیت انجام شد و Session ذخیره گردید.",
            }
        except Exception as exc:
            return {"status": "error", "error": f"خطا در بررسی رمز دومرحله‌ای: {exc}"}

    async def get_account_status(self) -> Dict[str, Any]:
        """Who is this Telethon session actually logged into? (Truthful status
        for the owner — no guessing from chat history.)"""
        if not self.client or not self.is_connected:
            return {"status": "not_connected", "error": "حساب کاربری تلگرام (یوزربات) متصل نیست."}
        try:
            me = await self.client.get_me()
            return {
                "status": "ok",
                "user_id": getattr(me, "id", None),
                "username": getattr(me, "username", "") or "",
                "phone": getattr(me, "phone", "") or "",
            }
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    async def send_message_to_owner(self, text: str, username: Optional[str] = None) -> Dict[str, Any]:
        """
        Send a message FROM the connected Telegram user account (userbot) TO
        the owner as a real DM — NOT to Saved Messages.

        Telethon cannot send to a bare numeric user id that is not in the
        session's entity cache ("PeerUser(...) not found"), so resolve the
        recipient properly:
        1. by @username (API username lookup, works even if never cached),
        2. from the account's contacts by numeric id,
        3. the raw numeric id (only works if already cached).
        'me' (Saved Messages) is used ONLY as a last resort for a generic
        connectivity test when no username was given — never silently when
        the user asked for a DM to their account, because a message to
        yourself always lands in Saved Messages anyway (Telegram has no
        self-DM chat), so falling back to it would lie about the destination.
        """
        if not self.client or not self.is_connected:
            return {"status": "not_connected", "error": "حساب کاربری تلگرام (یوزربات) متصل نیست. ابتدا لاگین کنید."}
        status = await self.get_account_status()
        connected_as = f"@{status.get('username')}" if status.get("username") else str(status.get("user_id", "نامشخص"))
        errors: List[str] = []

        if username:
            try:
                await self.client.send_message(f"@{username.lstrip('@')}", text)
                return {"status": "ok", "sent_via": f"@{username.lstrip('@')}", "connected_as": connected_as,
                        "message": "پیام از طریق اکانت تلگرام متصل (یوزربات) ارسال شد."}
            except Exception as exc:
                errors.append(f"@{username}: {exc}")

        # Resolve the owner id from contacts (works without a username).
        try:
            contacts = await self.client.get_contacts()
            owner_id = int(settings.TELEGRAM_OWNER_ID)
            for contact in contacts or []:
                if getattr(contact, "id", None) == owner_id:
                    await self.client.send_message(contact, text)
                    return {"status": "ok", "sent_via": "contacts", "connected_as": connected_as,
                            "message": "پیام از طریق اکانت تلگرام متصل (یوزربات) ارسال شد."}
        except Exception as exc:
            errors.append(f"contacts: {exc}")

        try:
            await self.client.send_message(int(settings.TELEGRAM_OWNER_ID), text)
            return {"status": "ok", "sent_via": "id", "connected_as": connected_as,
                    "message": "پیام از طریق اکانت تلگرام متصل (یوزربات) ارسال شد."}
        except Exception as exc:
            errors.append(f"id: {exc}")

        if not username:
            # Generic connectivity test only: Saved Messages is acceptable here,
            # and the result honestly says where it went.
            try:
                await self.client.send_message("me", text)
                return {"status": "ok", "sent_via": "me (Saved Messages)", "connected_as": connected_as,
                        "message": "پیام به «پیام‌های ذخیره‌شده» (Saved Messages) همان اکانت فرستاده شد."}
            except Exception as exc:
                errors.append(f"me: {exc}")

        logger.error(f"Failed to send message via user account: {errors}")
        note = ""
        if status.get("username") and username and status["username"].lower() == username.lstrip("@").lower():
            note = " توجه: سشن به همان اکانت شما وصل است و پیام به خودت در تلگرام همیشه در Saved Messages می‌افتد؛ چت دایرکت جداگانه‌ای با خودت وجود ندارد."
        return {
            "status": "error",
            "connected_as": connected_as,
            "error": (
                "تلگرام نتوانست گیرنده را پیدا کند (PeerUser not found). "
                f"سشن به حساب {connected_as} متصل است." + note +
                f" جزئیات: {'; '.join(errors)[:200]}"
            ),
        }

    async def list_accessible_channels(self, limit: int = 25) -> Dict[str, Any]:
        """Discovers channels that the connected Telegram user account has access to."""
        if not self.client or not self.is_connected:
            return {
                "status": "not_connected",
                "error": "ابتدا حساب کاربری تلگرام را از بخش اتصال Telethon متصل کنید.",
            }
        channels: List[Dict[str, Any]] = []
        try:
            async for dialog in self.client.iter_dialogs(limit=limit):
                if dialog.is_channel:
                    entity = dialog.entity
                    username = getattr(entity, "username", None) or str(dialog.id)
                    channels.append({
                        "id": str(dialog.id),
                        "title": dialog.name,
                        "username": username,
                    })
            return {"status": "ok", "channels": channels}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    async def fetch_latest_channel_media(self, channel_username: str, limit: int = 5) -> Dict[str, Any]:
        if not self.client or not self.is_connected:
            return {
                "status": "not_connected",
                "error": "حساب کاربری تلگرام (Telethon) متصل نیست. از دستور /channels یا منوی اتصال تلگرام اقدام کنید.",
            }

        clean_channel = channel_username.lstrip("@").strip()
        items: List[Dict[str, Any]] = []

        try:
            self.media_processor.enforce_storage_quota()
            async for msg in self.client.iter_messages(clean_channel, limit=limit):
                if msg.video or msg.photo:
                    ext = ".mp4" if msg.video else ".jpg"
                    target_file = Path(settings.TEMP_MEDIA_DIR) / f"{clean_channel}_{msg.id}{ext}"
                    downloaded = await msg.download_media(file=str(target_file))
                    if downloaded:
                        items.append({
                            "message_id": msg.id,
                            "channel": clean_channel,
                            "media_type": "video" if msg.video else "photo",
                            "file_path": str(downloaded),
                            "original_caption": msg.text or "",
                        })
            return {"status": "ok", "channel": clean_channel, "media_items": items}
        except Exception as exc:
            logger.error(f"Failed fetching media from channel {clean_channel}: {exc}")
            return {"status": "error", "error": str(exc)}

    async def stop(self) -> None:
        if self.client:
            await self.client.disconnect()
