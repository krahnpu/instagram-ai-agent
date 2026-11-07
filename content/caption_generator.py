from typing import Any, Dict
from ai.client import AIClient
from memory.long_term import LongTermMemory


class SmartCaptionGenerator:
    """Generates engaging, non-robotic captions and titles tailored to page style."""

    def __init__(self, ai_client: AIClient, lt_memory: LongTermMemory) -> None:
        self.ai_client = ai_client
        self.lt_memory = lt_memory

    async def generate_caption(self, topic_or_source_text: str, media_type: str = "reel") -> Dict[str, Any]:
        tone = await self.lt_memory.get_preference("page_tone_style", "حرفه‌ای، جذاب، صمیمی و تعاملی")
        prompt = (
            f"یک کپشن اینستاگرامی حرفه‌ای برای یک {media_type} با موضوع زیر بنویس:\n"
            f"موضوع / متن منبع: {topic_or_source_text}\n"
            f"لحن موردنظر پیج: {tone}\n"
            "ساختار خروجی باید شامل: ۱) قلاب (Hook) جذاب در خط اول ۲) متن اصلی روان ۳) دعوت به تعامل (CTA) در پایان باشد."
        )
        res = await self.ai_client.chat_completion(
            messages=[{"role": "user", "content": prompt}]
        )
        if "error" in res:
            return {"status": "error", "caption": topic_or_source_text}
        return {"status": "ok", "caption": res["message"].get("content", topic_or_source_text)}
