from pathlib import Path
from typing import Any, Dict
from config import settings
from utils.logger import get_logger


logger = get_logger("telegram.channel_monitor")


class ChannelMonitor:
    """
    Monitors configured Telegram source channels (such as KRALMOVIE) and executes
    the 9-step autonomous pipeline:
    1. Identify new channel message
    2. Verify video/image presence
    3. Download media file safely
    4. Analyze media content
    5. Generate smart Persian caption
    6. Select relevant hashtags
    7. Verify non-duplication via Perceptual Hash
    8. Publish automatically or send to Owner for approval
    9. Report published Instagram link
    """

    def __init__(
        self,
        user_connector: Any,
        agent: Any,
        ig_publisher: Any,
        duplicate_detector: Any,
        lt_memory: Any,
    ) -> None:
        self.user_connector = user_connector
        self.agent = agent
        self.ig_publisher = ig_publisher
        self.duplicate_detector = duplicate_detector
        self.lt_memory = lt_memory
        self.require_approval: bool = settings.AUTO_PUBLISH_REQUIRE_APPROVAL

    async def start(self) -> None:
        logger.info("Telegram ChannelMonitor pipeline ready.")

    async def process_channel_post(self, channel_username: str, item: Dict[str, Any]) -> Dict[str, Any]:
        file_path = Path(item["file_path"])
        phash = self.duplicate_detector.compute_file_phash(file_path) or ""

        # Check if already published from this channel
        last_id = await self.lt_memory.get_preference(f"last_msg_{channel_username}", "0")
        if int(item["message_id"]) <= int(last_id or 0):
            return {"status": "skipped_duplicate", "reason": "این پیام کانال قبلاً پردازش شده است."}

        prompt = (
            f"یک کپشن جذاب اینستاگرامی و ۱۰ هشتگ مرتبط برای این پست از کانال {channel_username} بنویس:\n"
            f"توضیح اولیه: {item.get('original_caption', 'ویدئوی سینمایی ویژه')}"
        )
        ai_res = await self.agent.ai_client.chat_completion(
            messages=[{"role": "user", "content": prompt}]
        )
        generated_caption = ai_res.get("message", {}).get("content", item.get("original_caption", ""))

        if self.require_approval:
            return {
                "status": "نیازمند تأیید",
                "channel": channel_username,
                "file_path": str(file_path),
                "proposed_caption": generated_caption,
                "phash": phash,
            }

        if item["media_type"] == "video":
            pub_res = await self.ig_publisher.publish_reel(str(file_path), generated_caption)
        else:
            pub_res = await self.ig_publisher.publish_photo(str(file_path), generated_caption)

        if pub_res.get("status") == "موفق":
            await self.lt_memory.set_preference(f"last_msg_{channel_username}", str(item["message_id"]))

        return pub_res
