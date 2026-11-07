from typing import Any, Optional
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from config import settings
from telegram.handlers import register_all_handlers
from telegram.rich_responder import TelegramRichResponder
from utils.logger import get_logger


logger = get_logger("telegram.bot")


class TelegramBotManager:
    """
    Primary Telegram Bot Manager built with Aiogram 3.x + Rich Message Draft Responder.
    Supports:
    - Zero-code bootstrap via `token.txt`
    - Interactive AI API onboarding on `/start` when no model is configured
    - Multi-model management via `/مدل` and `/ai`
    - Natural language Task Engine creation & management
    - Interactive Instagram Session JSON upload & format conversion
    - Interactive 5-step Telethon user login (`API_ID`, `API_HASH`, Phone, Code, 2FA)
    - Owner-governed package installation (`SelfExtenderManager`)
    - `<tg-thinking>Thinking…</tg-thinking>` via `sendRichMessageDraft` + `sendRichMessage`
    """

    def __init__(
        self,
        agent: Any,
        ig_client: Any,
        ig_analytics: Any,
        ig_content_manager: Any,
        ig_direct: Any,
        ig_comments: Any,
        ig_publisher: Any,
        ig_stories: Any,
        scheduler: Any,
        model_manager: Any,
        lt_memory: Any,
        channel_monitor: Any,
        user_connector: Optional[Any] = None,
        self_extender: Optional[Any] = None,
    ) -> None:
        self.agent = agent
        self.ig_client = ig_client
        self.ig_analytics = ig_analytics
        self.ig_content_manager = ig_content_manager
        self.ig_direct = ig_direct
        self.ig_comments = ig_comments
        self.ig_publisher = ig_publisher
        self.ig_stories = ig_stories
        self.scheduler = scheduler
        self.model_manager = model_manager
        self.lt_memory = lt_memory
        self.channel_monitor = channel_monitor
        self.user_connector = user_connector
        self.self_extender = self_extender
        self.rich_responder = TelegramRichResponder(settings.TELEGRAM_BOT_TOKEN)

        self.bot: Optional[Bot] = None
        self.dp = Dispatcher()

    async def start(self) -> None:
        if not settings.TELEGRAM_BOT_TOKEN or "YOUR_TELEGRAM_BOT_TOKEN" in settings.TELEGRAM_BOT_TOKEN:
            logger.error("TELEGRAM_BOT_TOKEN is not configured in token.txt or .env.")
            return

        self.bot = Bot(
            token=settings.TELEGRAM_BOT_TOKEN,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        register_all_handlers(self.dp, self)
        logger.info("Starting Telegram Bot polling...")
        await self.dp.start_polling(self.bot)

    async def notify_owner(self, message_text: str) -> None:
        if settings.TELEGRAM_OWNER_ID:
            try:
                await self.rich_responder.finalize_rich_message(
                    chat_id=settings.TELEGRAM_OWNER_ID,
                    rich_html=message_text,
                )
            except Exception as exc:
                logger.error(f"Failed to send notification to Owner: {exc}")

    async def stop(self) -> None:
        if self.bot:
            await self.bot.session.close()
