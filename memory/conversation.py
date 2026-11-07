from typing import Any, Dict, List
from memory.database import DatabaseManager


class ConversationMemory:
    """Stores, searches, and retrieves multi-turn user and DM conversations."""

    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    async def add_message(self, user_id: str, role: str, content: str, channel: str = "telegram") -> None:
        await self.db.execute(
            "INSERT INTO conversations (user_id, channel, role, content) VALUES (?, ?, ?, ?)",
            (user_id, channel, role, content),
        )

    async def get_recent_messages(self, user_id: str, limit: int = 12) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT role, content, channel, created_at FROM conversations WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        return list(reversed(rows))

    async def search_conversations(self, keyword: str, limit: int = 15) -> List[Dict[str, Any]]:
        return await self.db.fetch_all(
            "SELECT user_id, role, content, channel, created_at FROM conversations WHERE content LIKE ? ORDER BY id DESC LIMIT ?",
            (f"%{keyword}%", limit),
        )

    async def clear_user_history(self, user_id: str) -> None:
        await self.db.execute("DELETE FROM conversations WHERE user_id = ?", (user_id,))
