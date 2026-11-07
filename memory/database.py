from pathlib import Path
from typing import Any, Dict, List, Optional
import aiosqlite
from utils.logger import get_logger


logger = get_logger("memory.database")


class DatabaseManager:
    """
    Asynchronous SQLite database layer with dialect-agnostic SQL structure
    designed for seamless migration to PostgreSQL.
    """

    def __init__(self, db_url: str) -> None:
        self.db_url = db_url
        self.sqlite_path = db_url.replace("sqlite:///", "")
        self._conn: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        Path(self.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.sqlite_path)
        self._conn.row_factory = aiosqlite.Row

        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                channel TEXT NOT NULL DEFAULT 'telegram',
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS preferences (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS published_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                media_pk TEXT NOT NULL,
                media_code TEXT NOT NULL,
                media_type TEXT NOT NULL,
                caption TEXT,
                phash TEXT,
                source_channel TEXT,
                published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS deleted_posts_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                media_pk TEXT NOT NULL,
                reason TEXT,
                deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS replied_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                media_pk TEXT NOT NULL,
                comment_pk TEXT NOT NULL UNIQUE,
                reply_text TEXT NOT NULL,
                replied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS source_channels (
                channel_username TEXT PRIMARY KEY,
                auto_publish INTEGER DEFAULT 0,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS scheduled_jobs (
                job_id TEXT PRIMARY KEY,
                task_type TEXT NOT NULL,
                cron_expr TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ai_models (
                model_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                base_url TEXT NOT NULL,
                api_key_encrypted TEXT NOT NULL,
                model_name TEXT NOT NULL,
                temperature REAL DEFAULT 0.4,
                max_tokens INTEGER DEFAULT 1500,
                timeout INTEGER DEFAULT 45,
                is_active INTEGER DEFAULT 0,
                last_test_status TEXT DEFAULT 'verified',
                last_tested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS dynamic_tasks (
                task_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                natural_instruction TEXT NOT NULL,
                task_type TEXT NOT NULL,
                schedule_kind TEXT NOT NULL,
                interval_seconds INTEGER DEFAULT 0,
                cron_expr TEXT DEFAULT '',
                action_payload_json TEXT NOT NULL,
                require_admin_approval INTEGER DEFAULT 1,
                approved_by_owner INTEGER DEFAULT 0,
                enabled INTEGER DEFAULT 1,
                last_run_at TEXT DEFAULT '',
                last_status TEXT DEFAULT 'در انتظار تأیید ادمین',
                run_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS processed_events (
                event_key TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS package_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                package_name TEXT NOT NULL,
                version_spec TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL,
                installed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        await self._conn.commit()
        logger.info(f"Database initialized at {self.sqlite_path}")

    async def execute(self, query: str, params: tuple = ()) -> None:
        assert self._conn is not None
        await self._conn.execute(query, params)
        await self._conn.commit()

    async def fetch_all(self, query: str, params: tuple = ()) -> List[Dict[str, Any]]:
        assert self._conn is not None
        async with self._conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def fetch_one(self, query: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
        assert self._conn is not None
        async with self._conn.execute(query, params) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
