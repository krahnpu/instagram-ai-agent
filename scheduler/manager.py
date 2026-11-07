import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.date import DateTrigger
from memory.database import DatabaseManager
from scheduler.tasks import ScheduledTaskRunner
from utils.logger import get_logger


logger = get_logger("scheduler.manager")


PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")


class SchedulerManager:
    """
    Autonomous Natural-Language Task Engine & Persistent Job Scheduler.
    Supports:
    - Natural language task creation (e.g. "هر ۳۰ ثانیه دایرکت‌های اینستاگرام رو بررسی کن...")
    - One-shot, Interval (seconds/minutes/hours), Daily, Weekly, Cron, and Event-driven tasks
    - Owner confirmation workflow before activating newly created tasks
    - Event deduplication (`processed_events` table) so messages/comments are never processed twice
    - Automatic SQLite restoration after VPS restart
    - Full CRUD via Telegram (List, Pause, Resume, Edit interval/cron, Delete)
    - Immediate automatic pause & Owner alert on Instagram RateLimitError or Security Challenge
    """

    def __init__(
        self,
        db: DatabaseManager,
        agent: Any,
        ig_direct: Any,
        ig_comments: Any,
        ig_content_manager: Any,
        media_processor: Any,
    ) -> None:
        self.db = db
        self.scheduler = AsyncIOScheduler()
        self.runner = ScheduledTaskRunner(
            db=db,
            agent=agent,
            ig_direct=ig_direct,
            ig_comments=ig_comments,
            ig_content_manager=ig_content_manager,
            media_processor=media_processor,
            scheduler_manager=self,
        )
        self.notify_callback: Optional[Callable[[str], Coroutine[Any, Any, None]]] = None

    async def start(self, notify_callback: Callable[[str], Coroutine[Any, Any, None]]) -> None:
        self.notify_callback = notify_callback
        self.runner.set_notifier(notify_callback)

        # Ensure baseline maintenance tasks exist
        default_tasks = [
            {
                "task_id": "tsk_daily_channel",
                "title": "انتشار روزانه از کانال تلگرام (ساعت ۲۱)",
                "natural_instruction": "هر روز ساعت ۹ شب محتوای جدید کانال را بررسی و منتشر کن",
                "task_type": "channel_to_instagram_sync",
                "schedule_kind": "cron",
                "interval_seconds": 0,
                "cron_expr": "0 21 * * *",
                "approved_by_owner": 1,
                "enabled": 1,
            },
            {
                "task_id": "tsk_temp_cleanup",
                "title": "پاکسازی خودکار فایل‌های موقت هاست",
                "natural_instruction": "هر ۲ ساعت فایل‌های موقت بیش از سقف مجاز را پاکسازی کن",
                "task_type": "cleanup_temp_media",
                "schedule_kind": "interval",
                "interval_seconds": 7200,
                "cron_expr": "",
                "approved_by_owner": 1,
                "enabled": 1,
            },
        ]

        existing = await self.list_dynamic_tasks()
        existing_ids = {t["task_id"] for t in existing}
        for dt in default_tasks:
            if dt["task_id"] not in existing_ids:
                await self.db.execute(
                    """
                    INSERT INTO dynamic_tasks (
                        task_id, title, natural_instruction, task_type, schedule_kind,
                        interval_seconds, cron_expr, action_payload_json,
                        require_admin_approval, approved_by_owner, enabled, last_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 'فعال و بازیابی‌شده')
                    """,
                    (
                        dt["task_id"],
                        dt["title"],
                        dt["natural_instruction"],
                        dt["task_type"],
                        dt["schedule_kind"],
                        dt["interval_seconds"],
                        dt["cron_expr"],
                        json.dumps({"auto_reply": True}, ensure_ascii=False),
                        dt["approved_by_owner"],
                        dt["enabled"],
                    ),
                )

        # Restore all approved and enabled tasks from SQLite after host restart
        all_tasks = await self.list_dynamic_tasks()
        mounted_count = 0
        for task in all_tasks:
            if task["approved_by_owner"] and task["enabled"]:
                self._mount_dynamic_task(task)
                mounted_count += 1

        self.scheduler.start()
        logger.info(f"Persistent Task Engine started with {mounted_count} active restored tasks.")

    def parse_natural_language_task(self, text: str) -> Dict[str, Any]:
        """
        Parses Persian or English natural language scheduling instructions into a
        structured task blueprint ready for Owner confirmation.
        Example: «هر ۳۰ ثانیه دایرکت‌های اینستاگرام رو بررسی کن و جواب بده»
        """
        normalized = text.translate(PERSIAN_DIGITS).strip()
        lower = normalized.lower()

        # 1. Detect Task Type
        if any(k in lower for k in ("دایرکت", "پیام", "direct", "dm", "inbox")):
            task_type = "auto_reply_directs"
            title = "بررسی خودکار دایرکت‌ها و پاسخ هوشمند"
        elif any(k in lower for k in ("کامنت", "comment", "نظرات")):
            task_type = "auto_reply_comments"
            title = "بررسی کامنت‌های جدید و پاسخ هوشمند"
        elif any(k in lower for k in ("کانال", "channel", "تلگرام")):
            task_type = "channel_to_instagram_sync"
            title = "دریافت پست جدید از کانال تلگرام و انتشار در اینستاگرام"
        elif any(k in lower for k in ("تکراری", "duplicate")):
            task_type = "scan_duplicates"
            title = "بررسی دوره‌ای پست‌های تکراری پیج"
        else:
            task_type = "custom_agent_prompt"
            title = f"تسک هوشمند: {normalized[:42]}"

        # 2. Detect Schedule Kind & Interval / Cron
        schedule_kind = "interval"
        interval_seconds = 300
        cron_expr = ""

        sec_match = re.search(r"هر\s*(\d+)\s*ثانیه|every\s*(\d+)\s*sec", lower)
        min_match = re.search(r"هر\s*(\d+)\s*دقیقه|every\s*(\d+)\s*min", lower)
        hour_match = re.search(r"هر\s*(\d+)\s*ساعت|every\s*(\d+)\s*hour", lower)
        daily_hour_match = re.search(r"هر\s*روز\s*ساعت\s*(\d+)", lower)

        if sec_match:
            val = int(sec_match.group(1) or sec_match.group(2))
            interval_seconds = max(15, val)
            schedule_kind = "interval"
        elif min_match:
            val = int(min_match.group(1) or min_match.group(2))
            interval_seconds = max(30, val * 60)
            schedule_kind = "interval"
        elif hour_match:
            val = int(hour_match.group(1) or hour_match.group(2))
            interval_seconds = max(300, val * 3600)
            schedule_kind = "interval"
        elif daily_hour_match:
            hr = int(daily_hour_match.group(1)) % 24
            schedule_kind = "daily_cron"
            cron_expr = f"0 {hr} * * *"
            interval_seconds = 0
        elif "هفتگی" in lower or "هر هفته" in lower:
            schedule_kind = "weekly_cron"
            cron_expr = "0 10 * * 5"
            interval_seconds = 0
        elif "یک‌بار" in lower or "یکبار" in lower or "الان" in lower:
            schedule_kind = "one_shot"
            interval_seconds = 10
        elif "هر پست" in lower or "به محض" in lower:
            schedule_kind = "event_driven"
            interval_seconds = 45

        return {
            "title": title,
            "natural_instruction": text.strip(),
            "task_type": task_type,
            "schedule_kind": schedule_kind,
            "interval_seconds": interval_seconds,
            "cron_expr": cron_expr,
            "safety_note": (
                "توجه فنی: برای جلوگیری از محدودیت RateLimit اینستاگرام، صف پردازش، تأخیر ایمن و جدول "
                "عدم پردازش تکراری (processed_events) روی این تسک فعال است."
            ),
        }

    async def create_one_off_reminder(
        self, chat_id: int, delay_seconds: int, reminder_text: str
    ) -> Dict[str, Any]:
        """
        One-off reminder that actually sends a message to the owner after the
        requested delay (e.g. "message me in 30 seconds"). Uses a APScheduler
        'date' trigger; unlike recurring tasks it needs no approval tap.
        """
        delay = max(5, min(int(delay_seconds), 24 * 3600))
        run_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        job_id = f"rem_{uuid.uuid4().hex[:8]}"
        try:
            async def _fire() -> None:
                if self.notify_callback:
                    await self.notify_callback(reminder_text)

            self.scheduler.add_job(_fire, DateTrigger(run_date=run_at), id=job_id)
            return {"ok": True, "job_id": job_id, "run_at": run_at.isoformat(), "delay_seconds": delay}
        except Exception as exc:
            logger.error(f"Failed to schedule one-off reminder: {exc}")
            return {"ok": False, "error": str(exc)}

    async def create_task_from_natural_language(
        self,
        instruction: str,
        auto_approve: bool = False,
    ) -> Dict[str, Any]:
        spec = self.parse_natural_language_task(instruction)
        task_id = f"tsk_{uuid.uuid4().hex[:8]}"

        await self.db.execute(
            """
            INSERT INTO dynamic_tasks (
                task_id, title, natural_instruction, task_type, schedule_kind,
                interval_seconds, cron_expr, action_payload_json,
                require_admin_approval, approved_by_owner, enabled, last_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 1, ?)
            """,
            (
                task_id,
                spec["title"],
                spec["natural_instruction"],
                spec["task_type"],
                spec["schedule_kind"],
                spec["interval_seconds"],
                spec["cron_expr"],
                json.dumps({"instruction": spec["natural_instruction"]}, ensure_ascii=False),
                1 if auto_approve else 0,
                "فعال و در حال اجرا" if auto_approve else "نیازمند تأیید ادمین اصلی",
            ),
        )

        task_row = await self.get_task(task_id)
        if auto_approve and task_row:
            self._mount_dynamic_task(task_row)

        return {
            "status": "موفق" if auto_approve else "نیازمند تأیید",
            "task": task_row,
            "safety_note": spec["safety_note"],
        }

    async def approve_and_start_task(self, task_id: str) -> Dict[str, Any]:
        await self.db.execute(
            "UPDATE dynamic_tasks SET approved_by_owner = 1, enabled = 1, last_status = 'تأییدشده و فعال' WHERE task_id = ?",
            (task_id,),
        )
        task = await self.get_task(task_id)
        if not task:
            return {"status": "error", "error": "تسک موردنظر یافت نشد."}
        self._mount_dynamic_task(task)
        return {"status": "موفق", "task": task}

    async def pause_task(self, task_id: str) -> Dict[str, Any]:
        await self.db.execute(
            "UPDATE dynamic_tasks SET enabled = 0, last_status = 'متوقف‌شده توسط ادمین' WHERE task_id = ?",
            (task_id,),
        )
        if self.scheduler.running and self.scheduler.get_job(task_id):
            self.scheduler.remove_job(task_id)
        return {"status": "موفق", "task_id": task_id, "state": "paused"}

    async def resume_task(self, task_id: str) -> Dict[str, Any]:
        await self.db.execute(
            "UPDATE dynamic_tasks SET enabled = 1, approved_by_owner = 1, last_status = 'فعال' WHERE task_id = ?",
            (task_id,),
        )
        task = await self.get_task(task_id)
        if task:
            self._mount_dynamic_task(task)
        return {"status": "موفق", "task_id": task_id, "state": "active"}

    async def edit_task_schedule(
        self,
        task_id: str,
        interval_seconds: Optional[int] = None,
        cron_expr: Optional[str] = None,
    ) -> Dict[str, Any]:
        if interval_seconds is not None and interval_seconds > 0:
            await self.db.execute(
                "UPDATE dynamic_tasks SET schedule_kind = 'interval', interval_seconds = ?, cron_expr = '' WHERE task_id = ?",
                (max(15, int(interval_seconds)), task_id),
            )
        elif cron_expr:
            await self.db.execute(
                "UPDATE dynamic_tasks SET schedule_kind = 'cron', cron_expr = ?, interval_seconds = 0 WHERE task_id = ?",
                (cron_expr.strip(), task_id),
            )
        task = await self.get_task(task_id)
        if task and task["enabled"] and task["approved_by_owner"]:
            self._mount_dynamic_task(task)
        return {"status": "موفق", "task": task}

    async def delete_task(self, task_id: str) -> Dict[str, Any]:
        if self.scheduler.running and self.scheduler.get_job(task_id):
            self.scheduler.remove_job(task_id)
        await self.db.execute("DELETE FROM dynamic_tasks WHERE task_id = ?", (task_id,))
        return {"status": "موفق", "deleted_task_id": task_id}

    async def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one("SELECT * FROM dynamic_tasks WHERE task_id = ?", (task_id,))
        return self._format_row(row) if row else None

    async def list_dynamic_tasks(self) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all("SELECT * FROM dynamic_tasks ORDER BY created_at DESC")
        return [self._format_row(r) for r in rows]

    async def list_jobs(self) -> List[Dict[str, Any]]:
        """Backwards-compatible alias returning all dynamic tasks."""
        tasks = await self.list_dynamic_tasks()
        return [
            {
                "job_id": t["task_id"],
                "task_type": t["task_type"],
                "cron_expr": t["cron_expr"] or f"Every {t['interval_seconds']}s",
                "payload": {"note": t["title"], "instruction": t["natural_instruction"]},
                "enabled": t["enabled"] and t["approved_by_owner"],
                **t,
            }
            for t in tasks
        ]

    def _format_row(self, r: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "task_id": r["task_id"],
            "title": r["title"],
            "natural_instruction": r["natural_instruction"],
            "task_type": r["task_type"],
            "schedule_kind": r["schedule_kind"],
            "interval_seconds": int(r["interval_seconds"] or 0),
            "cron_expr": r["cron_expr"] or "",
            "approved_by_owner": bool(r["approved_by_owner"]),
            "enabled": bool(r["enabled"]),
            "last_run_at": r["last_run_at"] or "اجرا نشده",
            "last_status": r["last_status"] or "",
            "run_count": int(r["run_count"] or 0),
        }

    def _mount_dynamic_task(self, task: Dict[str, Any]) -> None:
        if not self.scheduler.running:
            pass
        task_id = task["task_id"]
        kind = task["schedule_kind"]

        if kind in ("interval", "event_driven") and task["interval_seconds"] > 0:
            trigger = IntervalTrigger(seconds=max(15, task["interval_seconds"]))
        elif kind in ("cron", "daily_cron", "weekly_cron") and task["cron_expr"]:
            parts = task["cron_expr"].split()
            if len(parts) == 5:
                trigger = CronTrigger(
                    minute=parts[0],
                    hour=parts[1],
                    day=parts[2],
                    month=parts[3],
                    day_of_week=parts[4],
                )
            else:
                trigger = IntervalTrigger(seconds=3600)
        elif kind == "one_shot":
            trigger = DateTrigger(run_date=datetime.now(timezone.utc))
        else:
            trigger = IntervalTrigger(seconds=max(30, task["interval_seconds"] or 300))

        self.scheduler.add_job(
            self.runner.run_dynamic_task,
            trigger=trigger,
            id=task_id,
            args=[task_id],
            replace_existing=True,
        )

    async def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
