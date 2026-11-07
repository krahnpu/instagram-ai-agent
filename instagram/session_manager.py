import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from config import settings
from utils.logger import get_logger
from utils.security import SecretProtector


logger = get_logger("instagram.session_manager")


class SessionManager:
    """
    Autonomous Instagram Session Manager & Format Converter.
    Supports:
    - Direct Instagrapi native session JSON (`uuids`, `device_settings`, `authorization_data` or `cookies`)
    - Browser Cookie Export JSON arrays (EditThisCookie / Cookie-Editor format `[{'name': 'sessionid', 'value': '...'}]`)
    - Flat Cookie dictionary JSON (`{'sessionid': '...', 'csrftoken': '...'}`)
    - Explicit missing-field diagnostic reporting (never fakes a successful connection)
    - Encrypted storage at rest when SESSION_ENCRYPTION_KEY is set
    """

    DEFAULT_DEVICE = {
        "app_version": "312.0.0.34.111",
        "android_version": 34,
        "android_release": "14.0",
        "dpi": "480dpi",
        "resolution": "1080x2400",
        "manufacturer": "Samsung",
        "device": "SM-S918B",
        "model": "Galaxy S23 Ultra",
        "cpu": "qcom",
        "version_code": "549812345",
    }

    def __init__(self, default_path: Path) -> None:
        self.session_path: Path = default_path
        self.accounts_dir: Path = Path(settings.IG_ACCOUNTS_DIR)
        self.protector = SecretProtector(settings.SESSION_ENCRYPTION_KEY)

    def inspect_and_convert_raw_json(self, raw_text: str) -> Dict[str, Any]:
        """
        Inspects a user-uploaded JSON string from Telegram, detects its format,
        and converts it to an Instagrapi-compatible settings dictionary if valid.
        """
        try:
            parsed = json.loads(raw_text)
        except Exception as exc:
            return {
                "valid": False,
                "format": "invalid_json",
                "error": f"فایل ارسالی یک JSON معتبر نیست: {exc}",
                "required_format_hint": "یک فایل JSON حاوی خروجی Instagrapi یا آرایه کوکی‌های مرورگر (شامل کلید sessionid) ارسال کنید.",
            }

        # Case 1: Array of browser cookies (e.g. Cookie-Editor / EditThisCookie)
        if isinstance(parsed, list):
            cookie_map: Dict[str, str] = {}
            for item in parsed:
                if isinstance(item, dict) and "name" in item and "value" in item:
                    cookie_map[str(item["name"])] = str(item["value"])

            if "sessionid" not in cookie_map or not cookie_map["sessionid"]:
                return {
                    "valid": False,
                    "format": "browser_cookie_array_incomplete",
                    "missing_fields": ["sessionid"],
                    "error": "فرمت آرایه کوکی مرورگر شناسایی شد اما کلید حیاتی 'sessionid' در آن وجود ندارد.",
                    "required_format_hint": "لطفاً هنگام خروجی گرفتن کوکی از مرورگر مطمئن شوید وارد حساب اینستاگرام شده‌اید و مقدار sessionid موجود است.",
                }

            converted = self._build_instagrapi_dict_from_cookies(cookie_map)
            return {
                "valid": True,
                "format": "browser_cookie_array",
                "session_dict": converted,
                "message": "فرمت آرایه کوکی مرورگر شناسایی و با موفقیت به ساختار استاندارد Instagrapi تبدیل شد.",
            }

        # Case 2: JSON Object
        if isinstance(parsed, dict):
            # 2a. Native Instagrapi format
            has_instagrapi_keys = "uuids" in parsed and ("device_settings" in parsed or "authorization_data" in parsed or "cookies" in parsed)
            if has_instagrapi_keys:
                auth_data = parsed.get("authorization_data", {})
                cookies = parsed.get("cookies", {})
                has_token = bool(
                    auth_data.get("sessionid")
                    or cookies.get("sessionid")
                    or parsed.get("sessionid")
                    or parsed.get("uuids")
                )
                if not has_token:
                    return {
                        "valid": False,
                        "format": "instagrapi_incomplete",
                        "missing_fields": ["authorization_data.sessionid / cookies.sessionid"],
                        "error": "ساختار Instagrapi شناسایی شد اما فاقد اطلاعات احراز هویت (sessionid) است.",
                    }
                return {
                    "valid": True,
                    "format": "instagrapi_native",
                    "session_dict": parsed,
                    "message": "قالب استاندارد Session کتابخانه Instagrapi شناسایی شد.",
                }

            # 2b. Flat cookie dictionary {"sessionid": "...", "csrftoken": "..."}
            if "sessionid" in parsed and parsed["sessionid"]:
                str_cookies = {str(k): str(v) for k, v in parsed.items() if isinstance(v, (str, int))}
                converted = self._build_instagrapi_dict_from_cookies(str_cookies)
                return {
                    "valid": True,
                    "format": "flat_cookie_dict",
                    "session_dict": converted,
                    "message": "دیکشنری کوکی شناسایی و به ساختار کامل Instagrapi تبدیل شد.",
                }

            return {
                "valid": False,
                "format": "unknown_json_object",
                "missing_fields": ["sessionid", "uuids", "device_settings"],
                "error": "ساختار JSON ارسالی فاقد کلیدهای استاندارد Session اینستاگرام (sessionid یا uuids) است.",
                "required_format_hint": "فایل JSON باید شامل 'sessionid' (کوکی مرورگر) یا ساختار کامل خروجی Instagrapi باشد.",
            }

        return {
            "valid": False,
            "format": "unsupported_root_type",
            "error": "ساختار ریشه فایل JSON باید Object یا Array باشد.",
        }

    def _build_instagrapi_dict_from_cookies(self, cookies: Dict[str, str]) -> Dict[str, Any]:
        sessionid = cookies.get("sessionid", "")
        ds_user_id = cookies.get("ds_user_id", "")
        return {
            "uuids": {
                "phone_id": str(uuid.uuid4()),
                "uuid": str(uuid.uuid4()),
                "client_session_id": str(uuid.uuid4()),
                "advertising_id": str(uuid.uuid4()),
                "android_device_id": f"android-{uuid.uuid4().hex[:16]}",
            },
            "cookies": cookies,
            "authorization_data": {
                "ds_user_id": ds_user_id,
                "sessionid": sessionid,
            },
            "device_settings": dict(self.DEFAULT_DEVICE),
            "user_agent": (
                "Instagram 312.0.0.34.111 Android (34/14.0; 480dpi; 1080x2400; "
                "Samsung; SM-S918B; Galaxy S23 Ultra; qcom; en_US; 549812345)"
            ),
        }

    def load_session_dict(self, path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
        target = path or self.session_path
        if not target.exists():
            logger.warning(f"Session file does not exist at {target}")
            return None

        try:
            raw_bytes = target.read_bytes()
            decrypted_text = self.protector.decrypt_if_needed(raw_bytes)
            inspection = self.inspect_and_convert_raw_json(decrypted_text)
            if not inspection.get("valid"):
                return None
            return inspection["session_dict"]
        except Exception as exc:
            logger.error(f"Failed to parse session file at {target}: {exc}")
            return None

    def save_session_dict(self, session_data: Dict[str, Any], path: Optional[Path] = None) -> bool:
        target = path or self.session_path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            serialized = json.dumps(session_data, ensure_ascii=False, indent=2)
            payload = self.protector.encrypt_if_enabled(serialized)
            target.write_bytes(payload)
            os.chmod(target, 0o600)
            return True
        except Exception as exc:
            logger.error(f"Failed to save session file at {target}: {exc}")
            # Fallback: /etc/secrets on Render is read-only, so a Telegram-uploaded
            # session can never be saved there. Fall back to the writable /tmp path
            # and switch the runtime session path to it, instead of failing silently.
            fallback = Path("/tmp/ig_api/session.json")
            if target != fallback:
                try:
                    fallback.parent.mkdir(parents=True, exist_ok=True)
                    serialized = json.dumps(session_data, ensure_ascii=False, indent=2)
                    payload = self.protector.encrypt_if_enabled(serialized)
                    fallback.write_bytes(payload)
                    os.chmod(fallback, 0o600)
                    self.session_path = fallback
                    logger.info(f"Session saved to fallback writable path {fallback}")
                    return True
                except Exception as exc2:
                    logger.error(f"Failed to save session file at fallback {fallback}: {exc2}")
            return False

    def change_session_path(self, new_path: str) -> Path:
        self.session_path = Path(new_path)
        return self.session_path

    def list_available_sessions(self) -> List[str]:
        sessions = []
        if self.session_path.exists():
            sessions.append(str(self.session_path))
        if self.accounts_dir.exists():
            for p in self.accounts_dir.glob("*.json"):
                sessions.append(str(p))
        return sessions
