from typing import Any, Dict, List
from instagram.client import InstagramClientManager
from memory.long_term import LongTermMemory
from utils.logger import get_logger


logger = get_logger("instagram.comments")


class InstagramCommentManager:
    """
    Manages Instagram post comments (`media_comments`, `media_comment`).
    Detects questions, criticism, suggestions, and spam, and prevents duplicate replies.
    """

    def __init__(self, ig_client: InstagramClientManager, lt_memory: LongTermMemory) -> None:
        self.ig_client = ig_client
        self.lt_memory = lt_memory

    async def fetch_media_comments(self, media_pk: str, amount: int = 20) -> Dict[str, Any]:
        res = await self.ig_client.execute_safe("media_comments", media_id=media_pk, amount=amount)
        if res["status"] != "ok":
            return res

        comments = res["data"]
        replied_ids = await self.lt_memory.get_replied_comment_ids(media_pk)

        items: List[Dict[str, Any]] = []
        for c in comments:
            c_pk = str(c.pk)
            text = getattr(c, "text", "") or ""
            items.append({
                "comment_pk": c_pk,
                "username": getattr(c.user, "username", "unknown"),
                "text": text,
                "category": self._classify_comment(text),
                "already_replied": c_pk in replied_ids,
            })

        return {"status": "ok", "media_pk": media_pk, "comments": items}

    async def reply_to_comment(self, media_pk: str, comment_pk: str, text: str) -> Dict[str, Any]:
        replied_ids = await self.lt_memory.get_replied_comment_ids(media_pk)
        if str(comment_pk) in replied_ids:
            return {
                "status": "skipped_duplicate",
                "message": "به این کامنت قبلاً پاسخ داده شده است (جلوگیری از پاسخ تکراری).",
            }

        res = await self.ig_client.execute_safe(
            "media_comment",
            media_id=media_pk,
            text=text,
            replied_to_comment_id=int(comment_pk) if str(comment_pk).isdigit() else None,
        )
        if res["status"] != "ok":
            return res

        await self.lt_memory.mark_comment_replied(media_pk, str(comment_pk), text)
        await self.lt_memory.increment_counter("total_comments_replied")
        return {"status": "موفق", "media_pk": media_pk, "comment_pk": comment_pk, "reply": text}

    def _classify_comment(self, text: str) -> str:
        lower = text.lower()
        if any(w in lower for w in ("پیجم", "فالو", "تبلیغ", "ارزان")):
            return "اسپم"
        if "؟" in text or "?" in text or any(w in lower for w in ("چطور", "چند", "لینک", "اسم", "کجا")):
            return "سؤال"
        if any(w in lower for w in ("ضعیف", "بد بود", "ایراد", "مشکل", "خراب")):
            return "انتقاد"
        if any(w in lower for w in ("پیشنهاد", "بهتره", "کاش", "لطفاً بذارید")):
            return "پیشنهاد"
        return "تعامل مثبت"
