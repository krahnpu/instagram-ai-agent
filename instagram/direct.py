from typing import Any, Dict, List
from instagram.client import InstagramClientManager
from memory.conversation import ConversationMemory
from memory.long_term import LongTermMemory
from utils.logger import get_logger


logger = get_logger("instagram.direct")


class InstagramDirectManager:
    """
    Manages Instagram Direct Messages (DM) via Instagrapi (`direct_threads`, `direct_send`).
    Classifies incoming messages (Question, Spam/Promo, VIP Escalation, Normal),
    preserves multi-turn conversation history, and prevents duplicate automated replies.
    """

    SPAM_KEYWORDS = ("تبلیغات", "فالوور ارزان", "پکیج میلیاردی", "لینک در بیو", "crypto", "forex")
    ESCALATION_KEYWORDS = ("همکاری", "اسپانسر", "شکایت", "قرارداد", "مدیر پیج", "فاکتور")

    def __init__(
        self,
        ig_client: InstagramClientManager,
        conv_memory: ConversationMemory,
        lt_memory: LongTermMemory,
    ) -> None:
        self.ig_client = ig_client
        self.conv_memory = conv_memory
        self.lt_memory = lt_memory

    async def fetch_inbox(self, amount: int = 10) -> Dict[str, Any]:
        res = await self.ig_client.execute_safe("direct_threads", amount=amount)
        if res["status"] != "ok":
            return res

        threads = res["data"]
        formatted_threads: List[Dict[str, Any]] = []

        for t in threads:
            messages = getattr(t, "messages", []) or []
            last_msg = messages[0] if messages else None
            msg_text = getattr(last_msg, "text", "") if last_msg else ""
            users = [u.username for u in getattr(t, "users", [])]

            classification = self.classify_message(msg_text or "")
            formatted_threads.append({
                "thread_id": str(t.id),
                "users": users,
                "last_message_id": str(getattr(last_msg, "id", "")) if last_msg else "",
                "last_message_text": msg_text or "[رسانه / بدون متن]",
                "classification": classification,
                "needs_admin_escalation": classification == "نیازمند ارجاع به ادمین",
            })

        return {"status": "ok", "count": len(formatted_threads), "threads": formatted_threads}

    async def send_reply(self, thread_id: str, text: str) -> Dict[str, Any]:
        res = await self.ig_client.execute_safe(
            "direct_send",
            text=text,
            thread_ids=[int(thread_id)] if thread_id.isdigit() else [thread_id],
        )
        if res["status"] != "ok":
            return res

        await self.conv_memory.add_message(
            user_id=f"ig_dm_{thread_id}",
            role="assistant",
            content=text,
            channel="instagram_dm",
        )
        await self.lt_memory.increment_counter("total_dm_replied")
        return {"status": "موفق", "thread_id": thread_id, "sent_text": text}

    def classify_message(self, text: str) -> str:
        lower = text.lower()
        if any(k in lower for k in self.SPAM_KEYWORDS):
            return "اسپم / تبلیغاتی"
        if any(k in lower for k in self.ESCALATION_KEYWORDS):
            return "نیازمند ارجاع به ادمین"
        if "؟" in text or "?" in text or any(q in lower for q in ("چند", "قیمت", "چطور", "کی", "کجا", "نسخه")):
            return "سؤال کاربر"
        return "پیام عادی"
