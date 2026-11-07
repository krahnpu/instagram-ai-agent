import asyncio
import signal
import sys
from pathlib import Path

from config import settings
from utils.logger import setup_logger, get_logger
from memory.database import DatabaseManager
from memory.conversation import ConversationMemory
from memory.long_term import LongTermMemory
from ai.client import AIClient
from ai.model_manager import ModelManager
from search.engine import WebSearchEngine
from instagram.client import InstagramClientManager
from instagram.publisher import InstagramPublisher
from instagram.stories import InstagramStoryManager
from instagram.direct import InstagramDirectManager
from instagram.comments import InstagramCommentManager
from instagram.interactions import InstagramInteractionManager
from instagram.analytics import InstagramAnalytics
from instagram.music import InstagramMusicManager
from instagram.content_manager import InstagramContentManager
from content.duplicate_detector import DuplicateDetector
from content.media_processor import MediaProcessor
from core.tools import ToolRegistry
from core.agent import AutonomousAgent
from scheduler.manager import SchedulerManager
from telegram.bot import TelegramBotManager
from telegram.user_account import TelegramUserAccountConnector
from telegram.channel_monitor import ChannelMonitor


logger = get_logger("main")


class ApplicationOrchestrator:
    """Central orchestrator connecting all 11 subsystems of Instagram Autonomous AI Agent."""

    def __init__(self) -> None:
        self.db = DatabaseManager(settings.DATABASE_URL)
        self.conv_memory = ConversationMemory(self.db)
        self.lt_memory = LongTermMemory(self.db)

        self.model_manager = ModelManager(self.lt_memory)
        self.ai_client = AIClient(self.model_manager)
        self.search_engine = WebSearchEngine()

        self.ig_client = InstagramClientManager(
            session_path=Path(settings.IG_SESSION_PATH),
            lt_memory=self.lt_memory,
        )
        self.media_processor = MediaProcessor(Path(settings.TEMP_MEDIA_DIR))
        self.ig_publisher = InstagramPublisher(self.ig_client, self.lt_memory, self.media_processor)
        self.ig_stories = InstagramStoryManager(self.ig_client, self.lt_memory)
        self.ig_direct = InstagramDirectManager(self.ig_client, self.conv_memory, self.lt_memory)
        self.ig_comments = InstagramCommentManager(self.ig_client, self.lt_memory)
        self.ig_interactions = InstagramInteractionManager(self.ig_client, self.lt_memory)
        self.ig_analytics = InstagramAnalytics(self.ig_client, self.lt_memory)
        self.ig_music = InstagramMusicManager(self.ig_client)
        self.duplicate_detector = DuplicateDetector(self.db)
        self.ig_content_manager = InstagramContentManager(
            self.ig_client,
            self.duplicate_detector,
            self.ig_analytics,
            self.lt_memory,
        )

        self.user_connector = TelegramUserAccountConnector(self.lt_memory, self.media_processor)
        from core.self_extender import SelfExtenderManager
        self.self_extender = SelfExtenderManager(self.db)

        self.tool_registry = ToolRegistry(
            ig_client=self.ig_client,
            ig_publisher=self.ig_publisher,
            ig_stories=self.ig_stories,
            ig_direct=self.ig_direct,
            ig_comments=self.ig_comments,
            ig_interactions=self.ig_interactions,
            ig_analytics=self.ig_analytics,
            ig_music=self.ig_music,
            ig_content_manager=self.ig_content_manager,
            search_engine=self.search_engine,
            lt_memory=self.lt_memory,
            user_connector=self.user_connector,
            self_extender=self.self_extender,
        )

        self.agent = AutonomousAgent(
            ai_client=self.ai_client,
            tool_registry=self.tool_registry,
            conv_memory=self.conv_memory,
            lt_memory=self.lt_memory,
        )

        self.scheduler = SchedulerManager(
            db=self.db,
            agent=self.agent,
            ig_direct=self.ig_direct,
            ig_comments=self.ig_comments,
            ig_content_manager=self.ig_content_manager,
            media_processor=self.media_processor,
        )
        self.tool_registry.scheduler = self.scheduler

        self.channel_monitor = ChannelMonitor(
            user_connector=self.user_connector,
            agent=self.agent,
            ig_publisher=self.ig_publisher,
            duplicate_detector=self.duplicate_detector,
            lt_memory=self.lt_memory,
        )

        self.tg_bot = TelegramBotManager(
            agent=self.agent,
            ig_client=self.ig_client,
            ig_analytics=self.ig_analytics,
            ig_content_manager=self.ig_content_manager,
            ig_direct=self.ig_direct,
            ig_comments=self.ig_comments,
            ig_publisher=self.ig_publisher,
            ig_stories=self.ig_stories,
            scheduler=self.scheduler,
            model_manager=self.model_manager,
            lt_memory=self.lt_memory,
            channel_monitor=self.channel_monitor,
            user_connector=self.user_connector,
            self_extender=self.self_extender,
        )

        # After a successful Telethon login, send the .session file to the
        # owner as a backup: on Render Free the container disk is wiped on
        # every redeploy, so without this backup the login would be forgotten.
        async def _send_telethon_session_backup() -> None:
            try:
                from pathlib import Path as _Path
                from aiogram.types import FSInputFile as _FSInputFile

                session_path = _Path(settings.TG_SESSION_PATH)
                if not session_path.exists() or not self.tg_bot.bot:
                    return
                await self.tg_bot.bot.send_document(
                    settings.TELEGRAM_OWNER_ID,
                    _FSInputFile(str(session_path)),
                    caption=(
                        "💾 <b>بکاپ Session تلگرام</b>\n"
                        "این فایل را نگه دارید. اگر سرویس دوباره دیپلوی شد و لاگین تلگرام فراموش شد، "
                        "همین فایل را به ربات بفرستید تا بدون کد دوباره وصل شود."
                    ),
                )
            except Exception as exc:
                logger.error(f"Failed to send Telethon session backup: {exc}")

        self.user_connector.on_connected = _send_telethon_session_backup

        # Let the agent send files to the owner as real Telegram documents.
        # Pass the manager (not .bot): .bot is None until start() runs, and
        # the tools resolve it lazily at send time.
        self.tool_registry.set_file_sender(self.tg_bot)

    async def start(self) -> None:
        setup_logger()
        logger.info("Starting Instagram Autonomous AI Agent...")
        await self.db.initialize()
        await self.model_manager.load_from_memory()

        # Connect Instagram via pre-loaded Session (/tmp/ig_api/session.json)
        ig_status = await self.ig_client.initialize_session()
        logger.info(f"Instagram initialization status: {ig_status['status']} ({ig_status.get('username', 'N/A')})")

        # Start optional Telethon user client if configured
        if settings.TG_API_ID and settings.TG_API_HASH:
            await self.user_connector.start()
            await self.channel_monitor.start()

        # Start persistent job scheduler
        await self.scheduler.start(notify_callback=self.tg_bot.notify_owner)

        # Start Telegram Bot Manager
        await self.tg_bot.start()

    async def shutdown(self) -> None:
        logger.info("Shutting down Instagram Autonomous AI Agent gracefully...")
        await self.scheduler.stop()
        await self.user_connector.stop()
        await self.tg_bot.stop()
        await self.db.close()


async def main() -> None:
    app = ApplicationOrchestrator()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(app.shutdown()))

    await app.start()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
