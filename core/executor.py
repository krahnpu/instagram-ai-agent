import asyncio
from typing import Any, Dict
from config import settings
from utils.logger import get_logger


logger = get_logger("core.executor")


class ToolExecutor:
    """
    Executes selected agent tools with controlled retry logic and safety inspection.
    Never retries on Instagram security challenges or authentication errors.
    """

    NON_RETRYABLE_STATUSES = {
        "login_required",
        "challenge_required",
        "two_factor_required",
        "permission_denied",
        "unsupported",
    }

    def __init__(self, tool_registry: Any) -> None:
        self.tool_registry = tool_registry

    async def execute(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        max_attempts = max(1, settings.MAX_RETRIES)

        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(f"Executing tool '{tool_name}' (attempt {attempt}/{max_attempts})")
                result = await self.tool_registry.invoke(tool_name, arguments)
                status = str(result.get("status", "ok")).lower()

                if status in self.NON_RETRYABLE_STATUSES:
                    logger.warning(f"Tool '{tool_name}' returned non-retryable status: {status}")
                    return result

                return result
            except Exception as exc:
                logger.error(f"Error in tool '{tool_name}' on attempt {attempt}: {exc}")
                if attempt >= max_attempts:
                    return {
                        "status": "error",
                        "tool": tool_name,
                        "error": str(exc),
                    }
                await asyncio.sleep(2 * attempt)

        return {"status": "error", "tool": tool_name, "error": "Execution exhausted"}
