from typing import List
from ai.client import AIClient


class HashtagGenerator:
    """Generates relevant Persian and English hashtags for Instagram discovery."""

    def __init__(self, ai_client: AIClient) -> None:
        self.ai_client = ai_client

    async def suggest_hashtags(self, caption_or_topic: str, count: int = 12) -> List[str]:
        prompt = (
            f"بر اساس متن زیر، دقیقاً {count} هشتگ پربازدید و مرتبط (ترکیبی از فارسی و انگلیسی) تولید کن. "
            f"فقط هشتگ‌ها را با فاصله بنویس:\n{caption_or_topic}"
        )
        res = await self.ai_client.chat_completion(messages=[{"role": "user", "content": prompt}])
        if "error" in res:
            return ["#اکسپلور", "#اینستاگرام", "#ریلز", "#explore", "#reels"]
        raw = res["message"].get("content", "")
        tags = [w.strip() for w in raw.split() if w.startswith("#")]
        return tags[:count]
