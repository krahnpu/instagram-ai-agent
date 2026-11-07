from typing import Any, Dict
from instagram.client import InstagramClientManager


class InstagramMusicManager:
    """
    Extracts music and original audio metadata from Instagram Reels/posts
    when supported by Instagrapi (`media_info`).
    """

    def __init__(self, ig_client: InstagramClientManager) -> None:
        self.ig_client = ig_client

    async def get_media_audio_info(self, media_pk: str) -> Dict[str, Any]:
        res = await self.ig_client.execute_safe("media_info", media_pk=media_pk)
        if res["status"] != "ok":
            return res

        media = res["data"]
        clips_metadata = getattr(media, "clips_metadata", None) or {}
        if not isinstance(clips_metadata, dict) or not clips_metadata:
            return {
                "status": "Unsupported",
                "media_pk": media_pk,
                "message": "اطلاعات موسیقی یا Audio برای این پست موجود نیست یا پشتیبانی نمی‌شود.",
            }

        music_info = clips_metadata.get("music_info") or {}
        original_sound = clips_metadata.get("original_sound_info") or {}

        return {
            "status": "ok",
            "media_pk": media_pk,
            "music_info": music_info if music_info else "Unsupported",
            "original_sound_info": original_sound if original_sound else "Unsupported",
        }
