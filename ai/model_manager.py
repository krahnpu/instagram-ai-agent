import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
import httpx
from config import settings
from memory.long_term import LongTermMemory
from utils.security import SecretProtector
from utils.logger import get_logger


logger = get_logger("ai.model_manager")


class ModelManager:
    """
    Multi-API & Multi-Model Manager supporting:
    - Interactive first-time setup wizard on `/start` when no AI model is configured
    - Full management via `/مدل` (Add, Test Live Connection, List, Switch Active, Delete, Retest)
    - Encrypted storage of API keys in SQLite (`ai_models` table)
    - Global active model propagation across all agent subsystems
    """

    def __init__(self, lt_memory: LongTermMemory) -> None:
        self.lt_memory = lt_memory
        self.protector = SecretProtector(settings.SESSION_ENCRYPTION_KEY)
        self._active_config: Dict[str, Any] = {
            "model_id": "default",
            "title": "Default Model",
            "base_url": settings.AI_API_BASE_URL,
            "api_key": settings.AI_API_KEY,
            "model_name": settings.AI_MODEL_NAME,
            "temperature": settings.AI_TEMPERATURE,
            "max_tokens": settings.AI_MAX_TOKENS,
            "timeout": settings.AI_TIMEOUT,
        }

    def is_configured(self) -> bool:
        return bool(
            self._active_config.get("base_url")
            and self._active_config.get("api_key")
            and self._active_config.get("model_name")
        )

    @staticmethod
    def validate_base_url(url: str) -> Dict[str, Any]:
        clean = url.strip().rstrip("/")
        parsed = urlparse(clean)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return {
                "valid": False,
                "error": "آدرس Base URL معتبر نیست. باید با https:// یا http:// شروع شود (مثلاً https://api.inceptionlabs.ai/v1).",
            }
        return {"valid": True, "normalized_url": clean}

    async def test_model_connection(
        self,
        base_url: str,
        api_key: str,
        model_name: str,
        timeout: int = 25,
    ) -> Dict[str, Any]:
        """
        Sends a real OpenAI-compatible chat completion probe to verify:
        1. Base URL reachability
        2. API Key validity
        3. Model existence
        4. Non-empty response generation & schema compatibility
        Only models that pass this live test can be saved/activated.
        """
        url_check = self.validate_base_url(base_url)
        if not url_check["valid"]:
            return {"ok": False, "error": url_check["error"]}

        endpoint = f"{url_check['normalized_url']}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key.strip()}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model_name.strip(),
            "messages": [{"role": "user", "content": "Reply with OK"}],
            "max_tokens": 16,
            "temperature": 0.1,
        }

        try:
            async with httpx.AsyncClient(timeout=float(timeout)) as client:
                resp = await client.post(endpoint, headers=headers, json=payload)
                if resp.status_code == 401:
                    return {"ok": False, "error": "خطای احراز هویت (401): کلید API (API Key) نامعتبر است."}
                if resp.status_code == 404:
                    return {
                        "ok": False,
                        "error": f"خطای 404: مسیر API یا مدل '{model_name}' در سرویس مقصد یافت نشد.",
                    }
                resp.raise_for_status()
                data = resp.json()
                choices = data.get("choices", [])
                if not choices or "message" not in choices[0]:
                    return {
                        "ok": False,
                        "error": "فرمت پاسخ سرویس با استاندارد OpenAI Chat Completions سازگار نیست.",
                    }
                sample_reply = choices[0]["message"].get("content", "").strip()
                return {
                    "ok": True,
                    "sample_reply": sample_reply or "OK",
                    "normalized_url": url_check["normalized_url"],
                }
        except httpx.TimeoutException:
            return {"ok": False, "error": "زمان انتظار (Timeout) به پایان رسید؛ آدرس Base URL پاسخ نمی‌دهد."}
        except Exception as exc:
            return {"ok": False, "error": f"خطا در تست اتصال واقعی به API: {exc}"}

    async def load_from_memory(self) -> None:
        rows = await self.lt_memory.db.fetch_all(
            "SELECT * FROM ai_models ORDER BY is_active DESC, last_tested_at DESC"
        )
        if rows:
            active_row = next((r for r in rows if r["is_active"] == 1), rows[0])
            self._apply_row_to_runtime(active_row)
            return

        # Seed initial model from .env if provided
        if settings.AI_API_BASE_URL and settings.AI_API_KEY and settings.AI_MODEL_NAME:
            await self.register_verified_model(
                base_url=settings.AI_API_BASE_URL,
                api_key=settings.AI_API_KEY,
                model_name=settings.AI_MODEL_NAME,
                set_active=True,
                skip_live_test=True,
            )

    def _apply_row_to_runtime(self, row: Dict[str, Any]) -> None:
        raw_enc = row["api_key_encrypted"].encode("utf-8")
        decrypted_key = self.protector.decrypt_if_needed(raw_enc)
        self._active_config = {
            "model_id": row["model_id"],
            "title": row["title"],
            "base_url": row["base_url"],
            "api_key": decrypted_key,
            "model_name": row["model_name"],
            "temperature": float(row.get("temperature", 0.4)),
            "max_tokens": int(row.get("max_tokens", 1500)),
            "timeout": int(row.get("timeout", 45)),
        }

    async def register_verified_model(
        self,
        base_url: str,
        api_key: str,
        model_name: str,
        title: Optional[str] = None,
        set_active: bool = True,
        skip_live_test: bool = False,
    ) -> Dict[str, Any]:
        if not skip_live_test:
            test_res = await self.test_model_connection(base_url, api_key, model_name)
            if not test_res["ok"]:
                return test_res
            base_url = test_res["normalized_url"]

        model_id = f"mdl_{uuid.uuid4().hex[:8]}"
        display_title = title or f"{model_name} ({urlparse(base_url).netloc})"
        encrypted_key = self.protector.encrypt_if_enabled(api_key.strip()).decode("utf-8")

        if set_active:
            await self.lt_memory.db.execute("UPDATE ai_models SET is_active = 0")

        await self.lt_memory.db.execute(
            """
            INSERT INTO ai_models (
                model_id, title, base_url, api_key_encrypted, model_name,
                temperature, max_tokens, timeout, is_active, last_test_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'متصل و تأییدشده')
            """,
            (
                model_id,
                display_title,
                base_url.strip().rstrip("/"),
                encrypted_key,
                model_name.strip(),
                settings.AI_TEMPERATURE,
                settings.AI_MAX_TOKENS,
                settings.AI_TIMEOUT,
                1 if set_active else 0,
            ),
        )

        await self.load_from_memory()
        return {
            "ok": True,
            "model_id": model_id,
            "title": display_title,
            "model_name": model_name.strip(),
            "base_url": base_url.strip().rstrip("/"),
            "is_active": set_active,
        }

    async def list_models(self) -> List[Dict[str, Any]]:
        rows = await self.lt_memory.db.fetch_all(
            "SELECT model_id, title, base_url, model_name, is_active, last_test_status, last_tested_at FROM ai_models ORDER BY is_active DESC, last_tested_at DESC"
        )
        return [
            {
                "model_id": r["model_id"],
                "title": r["title"],
                "base_url": r["base_url"],
                "model_name": r["model_name"],
                "is_active": bool(r["is_active"]),
                "last_test_status": r["last_test_status"],
                "last_tested_at": r["last_tested_at"],
            }
            for r in rows
        ]

    async def switch_active_model(self, model_id: str) -> Dict[str, Any]:
        row = await self.lt_memory.db.fetch_one("SELECT * FROM ai_models WHERE model_id = ?", (model_id,))
        if not row:
            return {"ok": False, "error": f"مدل با شناسه {model_id} یافت نشد."}

        await self.lt_memory.db.execute("UPDATE ai_models SET is_active = 0")
        await self.lt_memory.db.execute("UPDATE ai_models SET is_active = 1 WHERE model_id = ?", (model_id,))
        self._apply_row_to_runtime(row)
        logger.info(f"Switched global active AI model to {row['model_name']} ({row['base_url']})")
        return {"ok": True, "active_model": self.get_masked_config()}

    async def delete_model(self, model_id: str) -> Dict[str, Any]:
        await self.lt_memory.db.execute("DELETE FROM ai_models WHERE model_id = ?", (model_id,))
        await self.load_from_memory()
        return {"ok": True, "deleted_id": model_id}

    async def retest_saved_model(self, model_id: str) -> Dict[str, Any]:
        row = await self.lt_memory.db.fetch_one("SELECT * FROM ai_models WHERE model_id = ?", (model_id,))
        if not row:
            return {"ok": False, "error": "مدل موردنظر یافت نشد."}
        api_key = self.protector.decrypt_if_needed(row["api_key_encrypted"].encode("utf-8"))
        res = await self.test_model_connection(row["base_url"], api_key, row["model_name"])
        status_label = "متصل و تأییدشده" if res["ok"] else f"خطا: {res.get('error', '')[:60]}"
        await self.lt_memory.db.execute(
            "UPDATE ai_models SET last_test_status = ?, last_tested_at = CURRENT_TIMESTAMP WHERE model_id = ?",
            (status_label, model_id),
        )
        return res

    def get_config(self) -> Dict[str, Any]:
        return dict(self._active_config)

    async def update_config(self, **kwargs: Any) -> Dict[str, Any]:
        for k, v in kwargs.items():
            if k in self._active_config and v is not None:
                self._active_config[k] = v
        return self.get_masked_config()

    def get_masked_config(self) -> Dict[str, Any]:
        cfg = dict(self._active_config)
        raw_key = cfg.get("api_key", "")
        cfg["api_key"] = f"{raw_key[:5]}...{raw_key[-4:]}" if len(raw_key) > 9 else ("***" if raw_key else "تنظیم‌نشده")
        return cfg
