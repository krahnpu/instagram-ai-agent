from pathlib import Path
from typing import Any, Dict, List
from instagram.client import InstagramClientManager
from memory.long_term import LongTermMemory
from utils.logger import get_logger


logger = get_logger("instagram.publisher")


class InstagramPublisher:
    """
    Handles publishing Photos, Videos, Carousels (Albums), and Reels via Instagrapi,
    recording every published post in persistent audit history.
    """

    def __init__(self, ig_client: InstagramClientManager, lt_memory: LongTermMemory, media_processor=None) -> None:
        self.ig_client = ig_client
        self.lt_memory = lt_memory
        self.media_processor = media_processor

    def _resolve_thumbnail(self, video_path: Path, thumbnail: str | None) -> Path | None:
        """A Reel/video upload needs a cover thumbnail. Instagrapi generates
        one with MoviePy (needs an ffmpeg binary) when none is passed — that
        is what failed on the server. Resolve one without MoviePy: an explicit
        thumbnail first (e.g. Telegram's own video thumb), else extract a
        keyframe with the bundled ffmpeg binary."""
        if thumbnail:
            p = Path(thumbnail)
            if p.exists():
                return p
        if self.media_processor is not None:
            try:
                frame = self.media_processor.extract_video_keyframe(video_path)
                if frame is not None:
                    return frame
            except Exception as exc:
                logger.warning(f"Thumbnail keyframe extraction failed: {exc}")
        return None

    async def publish_photo(self, photo_path: str, caption: str) -> Dict[str, Any]:
        path = Path(photo_path)
        if not path.exists():
            return {"status": "error", "error": f"فایل تصویر یافت نشد: {photo_path}"}

        res = await self.ig_client.execute_safe("photo_upload", path=path, caption=caption)
        if res["status"] != "ok":
            return res

        media = res["data"]
        await self.lt_memory.log_published_post(
            media_pk=str(media.pk),
            media_code=str(media.code),
            media_type="photo",
            caption=caption,
        )
        return {
            "status": "موفق",
            "media_pk": str(media.pk),
            "code": str(media.code),
            "url": f"https://www.instagram.com/p/{media.code}/",
        }

    async def publish_video(self, video_path: str, caption: str, thumbnail: str | None = None) -> Dict[str, Any]:
        path = Path(video_path)
        if not path.exists():
            return {"status": "error", "error": f"فایل ویدئو یافت نشد: {video_path}"}

        thumb = self._resolve_thumbnail(path, thumbnail)
        kwargs: Dict[str, Any] = {"path": path, "caption": caption}
        if thumb is not None:
            kwargs["thumbnail"] = thumb
        res = await self.ig_client.execute_safe("video_upload", **kwargs)
        if res["status"] != "ok":
            return res

        media = res["data"]
        await self.lt_memory.log_published_post(
            media_pk=str(media.pk),
            media_code=str(media.code),
            media_type="video",
            caption=caption,
        )
        return {
            "status": "موفق",
            "media_pk": str(media.pk),
            "code": str(media.code),
            "url": f"https://www.instagram.com/p/{media.code}/",
        }

    async def publish_reel(self, video_path: str, caption: str, thumbnail: str | None = None) -> Dict[str, Any]:
        path = Path(video_path)
        if not path.exists():
            return {"status": "error", "error": f"فایل Reel یافت نشد: {video_path}"}

        thumb = self._resolve_thumbnail(path, thumbnail)
        kwargs: Dict[str, Any] = {"path": path, "caption": caption}
        if thumb is not None:
            kwargs["thumbnail"] = thumb
        res = await self.ig_client.execute_safe("clip_upload", **kwargs)
        if res["status"] != "ok":
            return res

        media = res["data"]
        await self.lt_memory.log_published_post(
            media_pk=str(media.pk),
            media_code=str(media.code),
            media_type="reel",
            caption=caption,
        )
        return {
            "status": "موفق",
            "media_pk": str(media.pk),
            "code": str(media.code),
            "url": f"https://www.instagram.com/reel/{media.code}/",
        }

    async def publish_carousel(self, file_paths: List[str], caption: str) -> Dict[str, Any]:
        paths = [Path(p) for p in file_paths if Path(p).exists()]
        if len(paths) < 2:
            return {"status": "error", "error": "پست چنداسلایدی (Carousel) نیازمند حداقل ۲ فایل معتبر است."}

        res = await self.ig_client.execute_safe("album_upload", paths=paths, caption=caption)
        if res["status"] != "ok":
            return res

        media = res["data"]
        await self.lt_memory.log_published_post(
            media_pk=str(media.pk),
            media_code=str(media.code),
            media_type="carousel",
            caption=caption,
        )
        return {
            "status": "موفق",
            "media_pk": str(media.pk),
            "code": str(media.code),
            "url": f"https://www.instagram.com/p/{media.code}/",
        }
