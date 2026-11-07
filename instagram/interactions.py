from typing import Any, Dict
from instagram.client import InstagramClientManager
from memory.long_term import LongTermMemory


class InstagramInteractionManager:
    """
    Manages controlled, non-spammy interactions (media likes, story reactions)
    only when explicitly permitted by the installed Instagrapi version.
    """

    def __init__(self, ig_client: InstagramClientManager, lt_memory: LongTermMemory) -> None:
        self.ig_client = ig_client
        self.lt_memory = lt_memory

    async def like_media(self, media_pk: str) -> Dict[str, Any]:
        res = await self.ig_client.execute_safe("media_like", media_id=media_pk)
        if res["status"] != "ok":
            return res
        return {"status": "موفق", "media_pk": media_pk, "action": "liked"}

    async def react_to_story(self, story_pk: str, reaction_text: str) -> Dict[str, Any]:
        # Verify if direct_story_send or story_like is supported
        if self.ig_client.has_method("story_like"):
            return await self.ig_client.execute_safe("story_like", story_id=story_pk)
        return {
            "status": "Unsupported",
            "error": "متد واکنش مستقیم به استوری در نسخه فعلی Instagrapi پشتیبانی نمی‌شود.",
        }
