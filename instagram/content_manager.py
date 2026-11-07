from datetime import datetime, timezone
from typing import Any, Dict, List
from instagram.client import InstagramClientManager
from instagram.analytics import InstagramAnalytics
from content.duplicate_detector import DuplicateDetector
from memory.long_term import LongTermMemory
from utils.logger import get_logger


logger = get_logger("instagram.content_manager")


class InstagramContentManager:
    """
    Coordinates duplicate post detection (exact vs suspicious), low-view post
    filtering with age thresholds, pre-deletion backup logging, and Owner-confirmed deletion.
    """

    def __init__(
        self,
        ig_client: InstagramClientManager,
        duplicate_detector: DuplicateDetector,
        analytics: InstagramAnalytics,
        lt_memory: LongTermMemory,
    ) -> None:
        self.ig_client = ig_client
        self.duplicate_detector = duplicate_detector
        self.analytics = analytics
        self.lt_memory = lt_memory

    async def list_recent_media(self, amount: int = 12) -> Dict[str, Any]:
        if not self.ig_client.is_connected:
            return {"status": "login_required", "error": "حساب اینستاگرام متصل نیست."}

        user_res = await self.ig_client.execute_safe("user_info_by_username", self.ig_client.active_username)
        if user_res["status"] != "ok":
            return user_res

        medias_res = await self.ig_client.execute_safe("user_medias", user_id=user_res["data"].pk, amount=amount)
        if medias_res["status"] != "ok":
            return medias_res

        posts = []
        for m in medias_res["data"]:
            posts.append({
                "pk": str(m.pk),
                "code": str(m.code),
                "url": f"https://www.instagram.com/p/{m.code}/",
                "caption": (getattr(m, "caption_text", "") or "")[:140],
                "likes": getattr(m, "like_count", 0) or 0,
                "comments": getattr(m, "comment_count", 0) or 0,
                "views": getattr(m, "view_count", None) or getattr(m, "play_count", None) or "Unsupported",
                "taken_at": str(getattr(m, "taken_at", "")),
                "thumbnail_url": str(getattr(m, "thumbnail_url", "") or ""),
            })
        return {"status": "ok", "count": len(posts), "posts": posts}

    async def find_duplicate_posts(self, amount: int = 25) -> Dict[str, Any]:
        listing = await self.list_recent_media(amount=amount)
        if listing["status"] != "ok":
            return listing

        report = await self.duplicate_detector.analyze_instagram_posts(listing["posts"])
        return {
            "status": "نیازمند تأیید",
            "total_scanned": len(listing["posts"]),
            "exact_duplicates": report["exact_duplicates"],
            "suspicious_duplicates": report["suspicious_duplicates"],
            "message": "فهرست پست‌های تکراری و مشکوک آماده شد. برای حذف، تأیید صریح ادمین الزامی است.",
        }

    async def find_low_performing_posts(self, min_views: int = 1000, min_age_days: int = 30) -> Dict[str, Any]:
        listing = await self.list_recent_media(amount=30)
        if listing["status"] != "ok":
            return listing

        now = datetime.now(timezone.utc)
        candidates: List[Dict[str, Any]] = []
        unsupported_views_count = 0

        for post in listing["posts"]:
            views = post["views"]
            if views == "Unsupported":
                unsupported_views_count += 1
                continue

            age_days = 0
            if post["taken_at"]:
                try:
                    dt = datetime.fromisoformat(post["taken_at"].replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    age_days = (now - dt).days
                except ValueError:
                    age_days = 0

            if int(views) < min_views and age_days >= min_age_days:
                candidates.append({
                    **post,
                    "age_days": age_days,
                    "threshold_min_views": min_views,
                })

        return {
            "status": "نیازمند تأیید",
            "min_views_threshold": min_views,
            "min_age_days": min_age_days,
            "candidates": candidates,
            "unsupported_metric_posts_skipped": unsupported_views_count,
        }

    async def delete_posts(self, media_pks: List[str], confirmed: bool = False) -> Dict[str, Any]:
        if not confirmed:
            return {
                "status": "نیازمند تأیید",
                "error": "عملیات حذف گروهی نیازمند تأیید صریح ادمین (confirmed_by_owner=True) است.",
                "pending_media_pks": media_pks,
            }

        deleted: List[str] = []
        failed: List[Dict[str, str]] = []

        for pk in media_pks:
            # Record audit backup prior to deletion
            await self.lt_memory.log_deleted_post_backup(media_pk=str(pk), reason="Owner confirmed deletion")
            res = await self.ig_client.execute_safe("media_delete", media_id=str(pk))
            if res["status"] == "ok":
                deleted.append(str(pk))
            else:
                failed.append({"media_pk": str(pk), "error": res.get("error", "Unknown error")})

        return {
            "status": "موفق" if deleted and not failed else "ناموفق",
            "deleted_pks": deleted,
            "failed": failed,
        }
