import asyncio
import time
from typing import Any, Callable, Coroutine, Dict, Optional
import httpx
from config import settings
from utils.logger import get_logger


logger = get_logger("telegram.rich_responder")


class TelegramRichResponder:
    """
    Implements Telegram's Rich Message Draft streaming with `<tg-thinking>Thinking…</tg-thinking>`:
    1. Sends `sendChatAction` (`typing`).
    2. Calls `sendRichMessageDraft` with a consistent non-zero `draft_id` and
       `<tg-thinking>Thinking…</tg-thinking>`.
    3. Periodically refreshes the draft (every ~12s, well within the 30s ephemeral window)
       as intermediate step titles or rich text grow.
    4. Finalizes with `sendRichMessage` (dropping `<tg-thinking>`), and automatically
       falls back to standard `sendMessage` with `parse_mode='HTML'` if the client/server
       does not support Rich Messages.
    """

    def __init__(self, bot_token: Optional[str] = None) -> None:
        self.bot_token = bot_token or settings.TELEGRAM_BOT_TOKEN

    @property
    def base_url(self) -> str:
        token = self.bot_token or settings.TELEGRAM_BOT_TOKEN
        return f"https://api.telegram.org/bot{token}"

    async def api_call(self, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not (self.bot_token or settings.TELEGRAM_BOT_TOKEN):
            return {"ok": False, "description": "Missing Telegram Bot Token"}
        url = f"{self.base_url}/{method}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json=payload)
                data = resp.json()
                if not data.get("ok"):
                    logger.debug(f"Telegram API {method} notice: {data.get('description')}")
                return data
        except Exception as exc:
            logger.debug(f"Telegram API {method} exception: {exc}")
            return {"ok": False, "description": str(exc)}

    async def send_thinking_draft(
        self,
        chat_id: int,
        draft_id: int,
        thinking_label: str = "Thinking…",
        partial_html: str = "",
    ) -> Dict[str, Any]:
        """
        Sends or updates the ephemeral `<tg-thinking>` draft preview.
        Note: `<tg-thinking>` is ONLY valid inside `sendRichMessageDraft`.
        """
        await self.api_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        html_body = f"<tg-thinking>{thinking_label}</tg-thinking>{partial_html}"
        return await self.api_call(
            "sendRichMessageDraft",
            {
                "chat_id": chat_id,
                "draft_id": draft_id,
                "rich_message": {"html": html_body},
            },
        )

    async def finalize_rich_message(
        self,
        chat_id: int,
        rich_html: str,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Finalizes the response via `sendRichMessage` without `<tg-thinking>`.
        Falls back to standard `sendMessage` with HTML parse_mode if unsupported.
        """
        clean_html = rich_html.replace("<tg-thinking>Thinking…</tg-thinking>", "")
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "rich_message": {"html": clean_html},
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        result = await self.api_call("sendRichMessage", payload)
        if result.get("ok"):
            return result

        # Automatic fallback to standard HTML sendMessage
        fallback_payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": clean_html,
            "parse_mode": "HTML",
        }
        if reply_markup:
            fallback_payload["reply_markup"] = reply_markup
        return await self.api_call("sendMessage", fallback_payload)

    async def run_with_thinking_stream(
        self,
        chat_id: int,
        coro_factory: Callable[[Callable[[str], Coroutine[Any, Any, None]]], Coroutine[Any, Any, str]],
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Executes an asynchronous task while maintaining a live `<tg-thinking>Thinking…</tg-thinking>`
        draft on Telegram, refreshing every 10 seconds if long-running, and finalizing with
        `sendRichMessage` (or fallback `sendMessage`).
        """
        draft_id = int(time.time() * 1000) % 900000000 + 100000000
        latest_partial = ""
        running = True

        await self.send_thinking_draft(chat_id, draft_id, thinking_label="Thinking…", partial_html="")

        async def update_progress(partial_title_html: str) -> None:
            nonlocal latest_partial
            latest_partial = partial_title_html
            await self.send_thinking_draft(
                chat_id,
                draft_id,
                thinking_label="Thinking…",
                partial_html=latest_partial,
            )

        async def keep_alive_loop() -> None:
            while running:
                await asyncio.sleep(12.0)
                if running:
                    await self.send_thinking_draft(
                        chat_id,
                        draft_id,
                        thinking_label="Thinking…",
                        partial_html=latest_partial,
                    )

        refresher = asyncio.create_task(keep_alive_loop())
        try:
            final_html = await coro_factory(update_progress)
        finally:
            running = False
            refresher.cancel()

        return await self.finalize_rich_message(chat_id, final_html, reply_markup=reply_markup)
