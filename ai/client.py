from typing import Any, Dict, List, Optional
import httpx
from utils.logger import get_logger
from ai.model_manager import ModelManager


logger = get_logger("ai.client")


class AIClient:
    """
    OpenAI-compatible asynchronous HTTP client supporting dynamic Base URL,
    API Key, Model Name, Temperature, Max Tokens, Timeout, and Function Calling.
    Zero hardcoded model names.
    """

    def __init__(self, model_manager: ModelManager) -> None:
        self.model_manager = model_manager

    async def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        cfg = self.model_manager.get_config()
        base_url = cfg["base_url"].rstrip("/")
        endpoint = f"{base_url}/chat/completions"

        headers = {
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        }

        payload: Dict[str, Any] = {
            "model": cfg["model_name"],
            "messages": messages,
            "temperature": cfg["temperature"],
            "max_tokens": cfg["max_tokens"],
        }

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        try:
            async with httpx.AsyncClient(timeout=float(cfg["timeout"])) as client:
                response = await client.post(endpoint, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
                choice = data["choices"][0]["message"]
                return {"status": "ok", "message": choice, "usage": data.get("usage", {})}
        except httpx.HTTPStatusError as exc:
            body = exc.response.text or ""
            logger.error(f"AI HTTP error {exc.response.status_code}: {body[:250]}")
            # The model provider refuses some topics (e.g. conflict/military
            # news) with a content filter. Surface that as a typed error so
            # the agent can answer briefly in Persian instead of dumping the
            # raw provider JSON to the user — and never fabricate an answer.
            if "content_filter" in body:
                return {"error": "content_filter"}
            return {"error": f"HTTP {exc.response.status_code}: {body[:200]}"}
        except Exception as exc:
            logger.error(f"AI connection error: {exc}")
            return {"error": str(exc)}
