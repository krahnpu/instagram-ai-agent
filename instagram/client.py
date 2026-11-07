import asyncio
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from instagrapi import Client
from instagrapi.exceptions import (
    LoginRequired,
    ChallengeRequired,
    TwoFactorRequired,
    RateLimitError,
    ClientError,
)
from config import settings
from utils.logger import get_logger
from instagram.session_manager import SessionManager
from instagram.proxy_manager import ProxyManager
from memory.long_term import LongTermMemory


logger = get_logger("instagram.client")


class InstagramClientManager:
    """
    Centralized Instagrapi Client Manager.
    Handles session loading from /tmp/ig_api/session.json, consistent proxy routing,
    method existence verification, and strict security error trapping without
    ever attempting to bypass CAPTCHA/2FA or rotate IPs aggressively.
    """

    def __init__(self, session_path: Path, lt_memory: LongTermMemory) -> None:
        self.lt_memory = lt_memory
        self.session_manager = SessionManager(default_path=session_path)
        self.proxy_manager = ProxyManager()
        self.client: Client = Client()
        self.client.delay_range = [settings.ACTION_MIN_DELAY_SEC, settings.ACTION_MAX_DELAY_SEC]
        self.is_connected: bool = False
        self.security_halted: bool = False
        self.active_username: Optional[str] = None
        self.last_error: Optional[str] = None

    async def initialize_session(self, custom_path: Optional[Path] = None) -> Dict[str, Any]:
        """Load pre-existing session from /tmp/ig_api/session.json and validate without re-login if valid."""
        target_path = custom_path or self.session_manager.session_path

        # Apply consistent proxy before any network request
        proxy_dsn = self.proxy_manager.get_proxy_dsn()
        if proxy_dsn:
            self.client.set_proxy(proxy_dsn)
            logger.info(f"Configured consistent proxy for Instagrapi: {self.proxy_manager.get_masked_proxy()}")

        loaded_settings = self.session_manager.load_session_dict(target_path)
        if not loaded_settings:
            self.is_connected = False
            self.last_error = f"فایل Session در مسیر {target_path} یافت نشد یا نامعتبر است."
            return {"status": "missing_session", "error": self.last_error}

        def _validate_sync() -> Dict[str, Any]:
            self.client.set_settings(loaded_settings)
            # Validate session without triggering password login
            self.client.get_timeline_feed()
            account = self.client.account_info()
            self.session_manager.save_session_dict(self.client.get_settings(), target_path)
            return {
                "pk": str(account.pk),
                "username": account.username,
                "full_name": account.full_name,
            }

        try:
            info = await asyncio.to_thread(_validate_sync)
            self.is_connected = True
            self.security_halted = False
            self.active_username = info["username"]
            await self.lt_memory.set_preference("active_ig_account", info["username"])
            return {"status": "connected", **info}
        except (LoginRequired, ChallengeRequired, TwoFactorRequired) as sec_exc:
            self.is_connected = False
            self.security_halted = True
            self.last_error = f"نیازمند تأیید هویت مجدد ادمین ({sec_exc.__class__.__name__})"
            logger.error(f"Instagram session requires admin intervention: {sec_exc.__class__.__name__}")
            return {"status": "security_halt", "error": self.last_error}
        except Exception as exc:
            self.is_connected = False
            self.last_error = str(exc)
            logger.error(f"Instagram session validation error: {exc}")
            return {"status": "error", "error": str(exc)}

    async def import_and_verify_session_json(self, raw_json_text: str) -> Dict[str, Any]:
        """
        Receives raw JSON uploaded by the Owner via Telegram, validates/converts its
        structure, tests live connection via Instagrapi, and persists securely if valid.
        """
        inspection = self.session_manager.inspect_and_convert_raw_json(raw_json_text)
        if not inspection.get("valid"):
            return {
                "status": "invalid_format",
                "format": inspection.get("format"),
                "missing_fields": inspection.get("missing_fields", []),
                "error": inspection.get("error"),
                "hint": inspection.get("required_format_hint", ""),
            }

        session_dict = inspection["session_dict"]
        backup_path = self.session_manager.accounts_dir / "previous_session_backup.json"
        if self.session_manager.session_path.exists():
            try:
                backup_path.write_bytes(self.session_manager.session_path.read_bytes())
            except OSError:
                pass

        self.session_manager.save_session_dict(session_dict, self.session_manager.session_path)
        validation = await self.initialize_session(self.session_manager.session_path)
        validation["detected_format"] = inspection["format"]
        validation["conversion_note"] = inspection.get("message", "")
        return validation

    def has_method(self, method_name: str) -> bool:
        """Inspect whether the installed version of Instagrapi supports a specific method."""
        return hasattr(self.client, method_name) and callable(getattr(self.client, method_name))

    async def execute_safe(self, method_name: str, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        """
        Execute an Instagrapi method safely inside a worker thread with full
        exception classification and automatic halt on security challenges.
        """
        if self.security_halted:
            return {
                "status": "security_halt",
                "error": "عملیات خودکار به دلیل محدودیت امنیتی یا نیاز به احراز هویت متوقف شده است.",
            }

        if not self.is_connected:
            return {
                "status": "login_required",
                "error": "حساب اینستاگرام متصل نیست. لطفاً فایل Session معتبر در /tmp/ig_api/session.json قرار دهید.",
            }

        if not self.has_method(method_name):
            logger.warning(f"Instagrapi method '{method_name}' is not supported in installed version.")
            return {
                "status": "Unsupported",
                "method": method_name,
                "error": f"متد '{method_name}' در نسخه فعلی Instagrapi پشتیبانی نمی‌شود.",
            }

        fn: Callable[..., Any] = getattr(self.client, method_name)

        try:
            data = await asyncio.to_thread(fn, *args, **kwargs)
            return {"status": "ok", "data": data}
        except LoginRequired as exc:
            self.is_connected = False
            self.security_halted = True
            self.last_error = "Session منقضی شده است (LoginRequired). نیازمند جایگزینی Session توسط ادمین."
            logger.error(f"LoginRequired during {method_name}: {exc}")
            return {"status": "login_required", "error": self.last_error}
        except ChallengeRequired as exc:
            self.security_halted = True
            self.last_error = "اینستاگرام درخواست ChallengeRequired داده است. فعالیت خودکار متوقف شد."
            logger.error(f"ChallengeRequired during {method_name}: {exc}")
            return {"status": "challenge_required", "error": self.last_error}
        except TwoFactorRequired as exc:
            self.security_halted = True
            self.last_error = "نیازمند کد احراز هویت دومرحله‌ای (TwoFactorRequired)."
            logger.error(f"TwoFactorRequired during {method_name}: {exc}")
            return {"status": "two_factor_required", "error": self.last_error}
        except RateLimitError as exc:
            self.last_error = "محدودیت نرخ درخواست اینستاگرام (RateLimitError). عملیات موقتاً متوقف شد."
            logger.warning(f"RateLimitError during {method_name}: {exc}")
            return {"status": "rate_limit", "error": self.last_error}
        except ClientError as exc:
            logger.error(f"Instagrapi ClientError during {method_name}: {exc}")
            return {"status": "client_error", "error": str(exc)}
        except Exception as exc:
            logger.error(f"Network/Unexpected error during {method_name}: {exc}")
            return {"status": "network_error", "error": str(exc)}
