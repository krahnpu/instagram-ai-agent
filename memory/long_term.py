from typing import Any, Dict, List, Optional, Set
from memory.database import DatabaseManager


class LongTermMemory:
    """
    Persistent Long-Term Memory storing Owner preferences, page writing style,
    published post history, deleted post audit backups, replied comment IDs,
    and monitored Telegram source channels. Supports search, edit, and delete.
    """

    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    async def set_preference(self, key: str, value: str) -> None:
        await self.db.execute(
            """
            INSERT INTO preferences (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """,
            (key, value),
        )

    async def get_preference(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = await self.db.fetch_one("SELECT value FROM preferences WHERE key = ?", (key,))
        return row["value"] if row else default

    async def delete_preference(self, key: str) -> None:
        await self.db.execute("DELETE FROM preferences WHERE key = ?", (key,))

    async def list_preferences(self) -> List[Dict[str, Any]]:
        return await self.db.fetch_all("SELECT key, value, updated_at FROM preferences ORDER BY key ASC")

    async def log_published_post(
        self,
        media_pk: str,
        media_code: str,
        media_type: str,
        caption: str,
        phash: str = "",
        source_channel: str = "",
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO published_posts (media_pk, media_code, media_type, caption, phash, source_channel)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (media_pk, media_code, media_type, caption, phash, source_channel),
        )
        await self.increment_counter("total_agent_published")

    async def log_deleted_post_backup(self, media_pk: str, reason: str) -> None:
        await self.db.execute(
            "INSERT INTO deleted_posts_audit (media_pk, reason) VALUES (?, ?)",
            (media_pk, reason),
        )

    async def mark_comment_replied(self, media_pk: str, comment_pk: str, reply_text: str) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO replied_comments (media_pk, comment_pk, reply_text) VALUES (?, ?, ?)",
            (media_pk, comment_pk, reply_text),
        )

    async def get_replied_comment_ids(self, media_pk: str) -> Set[str]:
        rows = await self.db.fetch_all(
            "SELECT comment_pk FROM replied_comments WHERE media_pk = ?",
            (media_pk,),
        )
        return {str(r["comment_pk"]) for r in rows}

    async def increment_counter(self, counter_key: str) -> int:
        current = await self.get_preference(counter_key, "0")
        next_val = int(current or "0") + 1
        await self.set_preference(counter_key, str(next_val))
        return next_val

    async def get_agent_counters(self) -> Dict[str, int]:
        published = int(await self.get_preference("total_agent_published", "0") or 0)
        dm_replied = int(await self.get_preference("total_dm_replied", "0") or 0)
        comments_replied = int(await self.get_preference("total_comments_replied", "0") or 0)
        return {
            "posts_published_by_agent": published,
            "direct_messages_replied": dm_replied,
            "comments_replied": comments_replied,
        }

    async def add_source_channel(self, channel_username: str, auto_publish: bool = False) -> None:
        clean = channel_username.lstrip("@").strip()
        await self.db.execute(
            """
            INSERT INTO source_channels (channel_username, auto_publish)
            VALUES (?, ?)
            ON CONFLICT(channel_username) DO UPDATE SET auto_publish = excluded.auto_publish
            """,
            (clean, 1 if auto_publish else 0),
        )

    async def remove_source_channel(self, channel_username: str) -> None:
        clean = channel_username.lstrip("@").strip()
        await self.db.execute("DELETE FROM source_channels WHERE channel_username = ?", (clean,))

    async def get_source_channels(self) -> List[Dict[str, Any]]:
        return await self.db.fetch_all("SELECT channel_username, auto_publish, added_at FROM source_channels")
