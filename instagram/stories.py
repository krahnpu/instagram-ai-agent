from pathlib import Path
from typing import Any, Dict
from instagram.client import InstagramClientManager
from memory.long_term import LongTermMemory


class InstagramStoryManager:
    """Manages publishing photo and video Stories via Instagrapi."""

    def __init__(self, ig_client: InstagramClientManager, lt_memory: LongTermMemory) -> None:
        self.ig_client = ig_client
        self.lt_memory = lt_memory

    async def publish_story(self, media_path: str, caption: str = "") -> Dict[str, Any]:
        path = Path(media_path)
        if not path.exists():
            return {"status": "error", "error": f"فایل استوری یافت نشد: {media_path}"}

        is_video = path.suffix.lower() in (".mp4", ".mov")
        method = "video_upload_to_story" if is_video else "photo_upload_to_story"

        res = await self.ig_client.execute_safe(method, path=path, caption=caption)
        if res["status"] != "ok":
            return res

        story = res["data"]
        await self.lt_memory.log_published_post(
            media_pk=str(story.pk),
            media_code=str(getattr(story, "code", story.pk)),
            media_type="story",
            caption=caption,
        )
        return {
            "status": "موفق",
            "story_pk": str(story.pk),
            "media_type": "video_story" if is_video else "photo_story",
        }
