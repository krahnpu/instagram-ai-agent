import re
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, Optional
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

    # Channel-to-Instagram sync: only these captions mark a video as an ad.
    _PROMO_MARKERS = ("تبلیغ", "تبلیغات", "پروموشن", "sponsored", "advertisement", "آگهی")
    # How many recent channel messages to inspect per run, and how many
    # reels to publish per run (cap avoids Instagram rate-limit bursts when
    # several new videos piled up; the rest follow on later runs).
    _CHANNEL_SYNC_FETCH_LIMIT = 10
    _CHANNEL_SYNC_MAX_POSTS = 3

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

            if task_type == "telegram_auto_reply":
                # Telegram-only task: checks the Telethon user account's DMs
                # and replies. It NEVER touches Instagram and is never halted
                # by the Instagram security guard — that guard paused every
                # Telegram task before, because the parser mapped them to
                # auto_reply_directs (Instagram) and the first run failed on
                # the missing IG session.
                connector = getattr(getattr(self.agent, "tool_registry", None), "user_connector", None)
                if connector is None or not getattr(connector, "is_connected", False):
                    await self._update_task_run(task_id, now_iso, "یوزربات تلگرام متصل نیست؛ دور بعد دوباره تلاش می‌شود")
                    return
                status = await connector.get_account_status()
                own_id = status.get("user_id")
                dialogs = await connector.list_telegram_dialogs(limit=20)
                if dialogs.get("status") != "ok":
                    await self._update_task_run(task_id, now_iso, f"خطا در خواندن گفت‌وگوها: {str(dialogs.get('error', ''))[:60]}")
                    return
                replied_count = 0
                for d in dialogs.get("dialogs", []):
                    if d.get("type") != "user" or not d.get("unread"):
                        continue
                    peer = d.get("username") or d.get("id")
                    msgs = await connector.read_telegram_messages(peer=peer, limit=min(5, int(d["unread"])))
                    if msgs.get("status") != "ok":
                        continue
                    for m in msgs.get("messages", []):
                        if not m.get("text"):
                            continue
                        if own_id and m.get("sender_id") == own_id:
                            continue
                        event_key = f"tg:{d['id']}:{m['id']}"
                        if await self._is_event_processed(event_key):
                            continue
                        prompt = (
                            "یک نفر در تلگرام به حساب کاربری من این پیام را فرستاده است:\n"
                            f"«{m.get('text', '')}»\n"
                            "یک پاسخ کوتاه، محترمانه و دقیق به فارسی بنویس."
                        )
                        ai_res = await self.agent.ai_client.chat_completion(
                            messages=[{"role": "user", "content": prompt}]
                        )
                        reply_text = ai_res.get("message", {}).get("content", "")
                        if reply_text:
                            send_res = await connector.send_telegram_message(peer=peer, text=reply_text)
                            if send_res.get("status") == "ok":
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

            if task_type == "channel_to_instagram_sync":
                status_text = await self._run_channel_sync(task_id, task)
                # None means the run already recorded its own status
                # (e.g. it halted the task on an Instagram security error).
                if status_text is not None:
                    await self._update_task_run(task_id, now_iso, status_text)
                return

            # Any other type has no runner implementation yet. Report that
            # honestly instead of a fake "موفق": a task that does nothing
            # must never look successful.
            await self._update_task_run(
                task_id, now_iso, f"خطا: اجرای تسک از نوع {task_type} هنوز پیاده نشده است"
            )
        except Exception as exc:
            logger.error(f"Task {task_id} execution error: {exc}")
            await self._update_task_run(task_id, now_iso, f"خطا: {str(exc)[:80]}")

    def _extract_channel(self, task: Dict[str, Any]) -> str:
        """Channel username for a sync task: stored payload first, then the
        task's own instruction text (@name, t.me/name, or 'کانال <name>')."""
        payload = task.get("action_payload")
        if isinstance(payload, dict):
            channel = str(payload.get("channel_username") or "").strip().lstrip("@")
            if channel:
                return channel
        text = task.get("natural_instruction") or ""
        for pattern in (r"@([A-Za-z0-9_]{5,})", r"t\.me/([A-Za-z0-9_]{5,})", r"کانال\s+([A-Za-z0-9_]{5,})"):
            match = re.search(pattern, text)
            if match:
                return match.group(1)
        return ""

    async def _run_channel_sync(self, task_id: str, task: Dict[str, Any]) -> Optional[str]:
        """Sync new videos from a Telegram channel to Instagram Reels.

        Only videos that were never processed are downloaded and published
        (deduplicated by message_id in processed_events), promo videos are
        skipped by caption, and the status always reports real counts —
        never a bare 'موفق' when nothing happened.
        """
        channel = self._extract_channel(task)
        if not channel:
            return (
                "خطا: کانال تلگرام برای این تسک مشخص نشده — "
                "دستور ساخت تسک باید نام کانال (مثل @meme_ol_ski) را داشته باشد"
            )

        registry = getattr(self.agent, "tool_registry", None)
        connector = getattr(registry, "user_connector", None)
        if connector is None or not getattr(connector, "is_connected", False):
            return "یوزربات تلگرام متصل نیست؛ دور بعد دوباره تلاش می‌شود"
        publisher = getattr(registry, "ig_publisher", None)
        if publisher is None:
            return "خطا: منتشرکننده اینستاگرام در دسترس نیست"

        listed = await connector.list_latest_channel_media(
            channel, limit=self._CHANNEL_SYNC_FETCH_LIMIT
        )
        if listed.get("status") != "ok":
            return f"خطا در خواندن کانال @{channel}: {str(listed.get('error', ''))[:80]}"

        # Oldest first so reels post in channel order.
        items = list(reversed(listed.get("media_items", [])))
        posted = 0
        skipped_seen = 0
        skipped_promo = 0
        skipped_photo = 0
        errors: list[str] = []
        to_post: list[tuple[int, str, str]] = []

        for item in items:
            message_id = item.get("message_id")
            if not message_id:
                continue
            event_key = f"ch:{channel}:{message_id}"
            if await self._is_event_processed(event_key):
                skipped_seen += 1
                continue
            if item.get("media_type") != "video":
                skipped_photo += 1
                await self._mark_event_processed(event_key, task_id)
                continue
            caption = item.get("caption") or ""
            if any(marker in caption for marker in self._PROMO_MARKERS):
                skipped_promo += 1
                await self._mark_event_processed(event_key, task_id)
                continue
            to_post.append((message_id, event_key, caption))

        for message_id, event_key, caption in to_post[: self._CHANNEL_SYNC_MAX_POSTS]:
            fetched = await connector.fetch_latest_channel_media(
                channel, limit=1, message_ids=[message_id]
            )
            if fetched.get("status") != "ok" or not fetched.get("media_items"):
                errors.append(f"{message_id}: {str(fetched.get('error', 'دریافت نشد'))[:40]}")
                continue
            media = fetched["media_items"][0]
            res = await publisher.publish_reel(
                media["file_path"], caption, thumbnail=media.get("thumbnail_path")
            )
            status = res.get("status", "")
            if status in self.SECURITY_STOP_STATUSES:
                await self._halt_on_security_error(task_id, res.get("error", status))
                return None
            if status == "موفق":
                posted += 1
                await self._mark_event_processed(event_key, task_id)
            else:
                errors.append(f"{message_id}: {str(res.get('error', status))[:40]}")

        if errors:
            return (
                f"خطا در انتشار {len(errors)} ویدئو ({posted} منتشر شد): "
                f"{'; '.join(errors)[:100]}"
            )
        return (
            f"موفق ({posted} ریل جدید منتشر شد، {skipped_seen} تکراری رد شد، "
            f"{skipped_promo} تبلیغاتی رد شد، {skipped_photo} عکس رد شد)"
        )

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
