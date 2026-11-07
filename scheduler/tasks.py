from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Optional
from memory.database import DatabaseManager
from utils.logger import get_logger


logger = get_logger("scheduler.tasks")


class ScheduledTaskRunner:
    """
    Executes background autonomous tasks created by the Task Engine,
    enforces event deduplication (`processed_events` table) so DMs/comments
    are never answered twice, and halts tasks immediately on Instagram security/rate limits.
    """

    SECURITY_STOP_STATUSES = {
        "login_required",
        "challenge_required",
        "two_factor_required",
        "rate_limit",
        "security_halt",
    }

    def __init__(
        self,
        db: DatabaseManager,
        agent: Any,
        ig_direct: Any,
        ig_comments: Any,
        ig_content_manager: Any,
        media_processor: Any,
        scheduler_manager: Any,
    ) -> None:
        self.db = db
        self.agent = agent
        self.ig_direct = ig_direct
        self.ig_comments = ig_comments
        self.ig_content_manager = ig_content_manager
        self.media_processor = media_processor
        self.scheduler_manager = scheduler_manager
        self._notifier: Optional[Callable[[str], Coroutine[Any, Any, None]]] = None

    def set_notifier(self, notifier: Callable[[str], Coroutine[Any, Any, None]]) -> None:
        self._notifier = notifier

    async def _is_event_processed(self, event_key: str) -> bool:
        row = await self.db.fetch_one(
            "SELECT event_key FROM processed_events WHERE event_key = ?",
            (event_key,),
        )
        return row is not None

    async def _mark_event_processed(self, event_key: str, task_id: str) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO processed_events (event_key, task_id) VALUES (?, ?)",
            (event_key, task_id),
        )

    async def run_dynamic_task(self, task_id: str) -> None:
        task = await self.scheduler_manager.get_task(task_id)
        if not task or not task["enabled"] or not task["approved_by_owner"]:
            return

        if self.agent.is_paused and task["task_type"] != "cleanup_temp_media":
            return

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        task_type = task["task_type"]
        logger.info(f"Executing dynamic task {task_id} ({task_type})")

        try:
            if task_type == "cleanup_temp_media":
                quota = self.media_processor.enforce_storage_quota()
                await self._update_task_run(task_id, now_iso, f"موفق (Temp: {quota['current_temp_mb']}MB)")
                return

            if task_type == "auto_reply_directs":
                inbox = await self.ig_direct.fetch_inbox(amount=8)
                status = inbox.get("status", "")
                if status in self.SECURITY_STOP_STATUSES:
                    await self._halt_on_security_error(task_id, inbox.get("error", status))
                    return

                replied_count = 0
                for thread in inbox.get("threads", []):
                    msg_id = thread.get("last_message_id") or ""
                    if not msg_id:
                        continue
                    event_key = f"dm:{thread['thread_id']}:{msg_id}"
                    if await self._is_event_processed(event_key):
                        continue

                    # NEVER reply to our own last message: the previous run's
                    # reply becomes the thread's last message, and replying to
                    # it again would spam the same thread every interval.
                    if not thread.get("last_message_is_incoming", True):
                        await self._mark_event_processed(event_key, task_id)
                        continue

                    if thread.get("classification") == "اسپم / تبلیغاتی":
                        await self._mark_event_processed(event_key, task_id)
                        continue

                    if thread.get("needs_admin_escalation") and self._notifier:
                        await self._notifier(
                            f"🔔 <b>ارجاع مکالمه مهم دایرکت (تسک {task_id}):</b>\n"
                            f"کاربر: @{', '.join(thread.get('users', []))}\n"
                            f"متن پیام: <i>{thread.get('last_message_text', '')}</i>"
                        )
                        await self._mark_event_processed(event_key, task_id)
                        continue

                    # Generate contextual AI response
                    prompt = (
                        f"کاربری در دایرکت اینستاگرام این پیام را فرستاده است:\n"
                        f"«{thread.get('last_message_text', '')}»\n"
                        "یک پاسخ کوتاه، محترمانه، دقیق و متناسب با محتوای پیام بنویس."
                    )
                    ai_res = await self.agent.ai_client.chat_completion(
                        messages=[{"role": "user", "content": prompt}]
                    )
                    reply_text = ai_res.get("message", {}).get("content", "")
                    if reply_text:
                        send_res = await self.ig_direct.send_reply(thread["thread_id"], reply_text)
                        if send_res.get("status") == "موفق":
                            replied_count += 1
                            await self._mark_event_processed(event_key, task_id)

                await self._update_task_run(
                    task_id, now_iso, f"موفق ({replied_count} پاسخ جدید ارسال شد)"
                )
                return

            if task_type == "scan_duplicates":
                scan = await self.ig_content_manager.find_duplicate_posts(amount=20)
                dup_count = len(scan.get("exact_duplicates", []))
                await self._update_task_run(task_id, now_iso, f"موفق ({dup_count} مورد تکراری)")
                if dup_count > 0 and self._notifier:
                    await self._notifier(
                        f"🔍 تسک {task['title']}: تعداد {dup_count} پست تکراری شناسایی شد و منتظر تأیید شماست."
                    )
                return

            await self._update_task_run(task_id, now_iso, "موفق")
        except Exception as exc:
            logger.error(f"Task {task_id} execution error: {exc}")
            await self._update_task_run(task_id, now_iso, f"خطا: {str(exc)[:80]}")

    async def _update_task_run(self, task_id: str, now_iso: str, status_text: str) -> None:
        await self.db.execute(
            """
            UPDATE dynamic_tasks
            SET last_run_at = ?, last_status = ?, run_count = run_count + 1
            WHERE task_id = ?
            """,
            (now_iso, status_text, task_id),
        )

    async def _halt_on_security_error(self, task_id: str, reason: str) -> None:
        await self.scheduler_manager.pause_task(task_id)
        await self.db.execute(
            "UPDATE dynamic_tasks SET last_status = ? WHERE task_id = ?",
            (f"متوقف‌شده به دلیل محدودیت امنیتی: {reason}", task_id),
        )
        if self._notifier:
            await self._notifier(
                f"⚠️ <b>توقف خودکار تسک {task_id} برای محافظت از اکانت اینستاگرام</b>\n"
                f"علت: <code>{reason}</code>\n"
                "لطفاً وضعیت Session را بررسی کرده و پس از رفع محدودیت، تسک را فعال کنید."
            )
