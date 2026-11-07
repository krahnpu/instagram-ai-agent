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

    async def start(self) -> None:
        creds = await self.get_saved_credentials()
        api_id = creds["api_id"]
        api_hash = creds["api_hash"]
        if not api_id or not api_hash:
            return
        try:
            session_file = Path(settings.TG_SESSION_PATH)
            session_file.parent.mkdir(parents=True, exist_ok=True)
            self.client = TelegramClient(str(session_file), api_id, api_hash)
            await self.client.connect()
            self.is_connected = await self.client.is_user_authorized()
            logger.info(f"Telethon user client initialized (authorized={self.is_connected})")
        except Exception as exc:
            logger.error(f"Telethon initialization error: {exc}")
            self.is_connected = False

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
            me = await self.client.get_me()
            return {
                "status": "connected",
                "username": getattr(me, "username", "") or str( getattr(me, "id", "")),
                "message": "اتصال حساب کاربری تلگرام با موفقیت انجام و Session به صورت پایدار ذخیره شد.",
            }
        except SessionPasswordNeededError:
            self._awaiting_2fa = True
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
            me = await self.client.get_me()
            return {
                "status": "connected",
                "username": getattr(me, "username", "") or str(getattr(me, "id", "")),
                "message": "احراز هویت دومرحله‌ای با موفقیت انجام شد و Session ذخیره گردید.",
            }
        except Exception as exc:
            return {"status": "error", "error": f"خطا در بررسی رمز دومرحله‌ای: {exc}"}

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
