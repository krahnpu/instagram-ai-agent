from io import BytesIO
import re
from pathlib import Path
from typing import Any, Dict
from aiogram import Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from config import settings
from core.permissions import PermissionManager
from telegram.keyboards import (
    build_main_menu_keyboard,
    build_back_to_menu_keyboard,
    build_confirmation_keyboard,
    build_models_menu_keyboard,
    build_tasks_menu_keyboard,
)
from telegram.admin import AdminCommandController

# --- Media helpers (module level, so they are unit-testable) -----------------
# What kind of media a message carries, and how to tell the agent about it.
# Used by the audio/video/document handlers and by reply-to-media
# resolution in the text handler, so "استخراجش کن" on a replied-to video
# and "آهنگش را پیدا کن" on a replied-to audio file work without the user
# having to send the file again.

_MEDIA_AUDIO_EXTS = (".mp3", ".ogg", ".oga", ".wav", ".m4a", ".flac", ".aac", ".opus")
_MEDIA_VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".webm", ".avi")
_MEDIA_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")


def _pick_message_media(msg: Any) -> tuple[Any, str, str] | None:
    """Return (file_object, kind, suffix) for a message's media, else None.
    kind is one of: video, audio, voice, photo."""
    if getattr(msg, "video", None):
        return msg.video, "video", ".mp4"
    if getattr(msg, "video_note", None):
        return msg.video_note, "video", ".mp4"
    if getattr(msg, "audio", None):
        return msg.audio, "audio", ".mp3"
    if getattr(msg, "voice", None):
        return msg.voice, "voice", ".ogg"
    photo = getattr(msg, "photo", None)
    if photo:
        return photo[-1], "photo", ".jpg"
    doc = getattr(msg, "document", None)
    if doc:
        name = (doc.file_name or "").lower()
        mime = (doc.mime_type or "").lower()
        if mime.startswith("video/") or name.endswith(_MEDIA_VIDEO_EXTS):
            return doc, "video", ".mp4"
        if mime.startswith("audio/") or name.endswith(_MEDIA_AUDIO_EXTS):
            return doc, "audio", ".mp3"
        if mime.startswith("image/") or name.endswith(_MEDIA_IMAGE_EXTS):
            return doc, "photo", ".jpg"
    return None


def _media_prompt(kind: str, path: Path, user_text: str, replied: bool = False) -> str:
    """Build the agent prompt for a media file the user sent or replied to.
    Always includes the saved path and tells the agent NOT to ask for the
    file again — the file is already here."""
    where = "روی آن ریپلای کرده" if replied else "فرستاده"
    if kind == "video":
        return (
            f"کاربر روی یک ویدئو {where} و گفته: «{user_text}». "
            f"فایل ویدئو در این مسیر ذخیره شده: {path}. "
            "همین فایل را پردازش کن؛ از کاربر نخواه دوباره آن را بفرستد. "
            "اگر خواست آهنگش را پیدا کنی از identify_song یا find_song_of_video استفاده کن، "
            "اگر خواست دانلودش کنی از download_media، و اگر خواست توصیفش کنی از describe_video."
        )
    if kind in ("audio", "voice"):
        return (
            f"کاربر یک فایل صوتی {where} و گفته: «{user_text}». "
            f"فایل صوتی در این مسیر ذخیره شده: {path}. "
            "آهنگ داخل آن را با identify_song پیدا کن و برایش بفرست؛ "
            "از کاربر نخواه دوباره آن را بفرستد."
        )
    return (
        f"کاربر یک عکس {where} و گفته: «{user_text}». "
        f"فایل عکس در این مسیر ذخیره شده: {path}. "
        "با describe_image توصیفش کن؛ از کاربر نخواه دوباره آن را بفرستد."
    )


def register_all_handlers(dp: Dispatcher, manager: Any) -> None:
    perms = PermissionManager()
    admin_ctrl = AdminCommandController(manager)

    # Per-user interactive wizard state (Owner onboarding, Telethon login, Instagram Session upload)
    wizard_states: Dict[int, Dict[str, Any]] = {}

    def _deny_if_not_owner(user_id: int) -> str | None:
        if not perms.is_owner(user_id):
            return (
                "⛔ <b>محدودیت دسترسی سمت سرور (Server-Side RBAC)</b>\n"
                "شما کاربر عادی هستید و اجازه دسترسی به تنظیمات، مدل‌های هوش مصنوعی، تسک‌ها یا Sessionها را ندارید.\n"
                "می‌توانید سؤال‌های خود را بپرسید تا ایجنت با جست‌وجوی به‌روز اینترنت پاسخ دهد."
            )
        return None

    async def _render_models_overview() -> str:
        models = await manager.model_manager.list_models()
        if not models:
            return (
                "🤖 <b>مدیریت چند API و مدل هوش مصنوعی (/مدل)</b>\n\n"
                "هنوز هیچ مدل تأییدشده‌ای ثبت نشده است. روی «➕ افزودن API و مدل جدید» بزنید."
            )
        lines = ["🤖 <b>فهرست APIها و مدل‌های هوش مصنوعی ثبت‌شده:</b>\n"]
        for m in models:
            active_tag = "🟢 <b>[مدل فعال کل پروژه]</b>" if m["is_active"] else "⚪️ آماده"
            lines.append(
                f"• <b>{m['model_name']}</b> {active_tag}\n"
                f"  آدرس: <code>{m['base_url']}</code>\n"
                f"  وضعیت تست: {m['last_test_status']}"
            )
        return "\n".join(lines)

    async def _render_tasks_overview() -> str:
        tasks = await manager.scheduler.list_dynamic_tasks()
        if not tasks:
            return "⏰ هیچ تسک زمان‌بندی‌شده‌ای ثبت نشده است."
        lines = ["⏰ <b>مدیریت تسک‌های خودکار پایدار (Task Engine):</b>\n"]
        for t in tasks:
            sched = t["cron_expr"] if t["cron_expr"] else f"هر {t['interval_seconds']} ثانیه"
            state = (
                "⏳ منتظر تأیید ادمین"
                if not t["approved_by_owner"]
                else ("🟢 فعال" if t["enabled"] else "⏸ متوقف")
            )
            lines.append(
                f"• <b>{t['title']}</b> (<code>{t['task_id']}</code>)\n"
                f"  زمان‌بندی: <code>{sched}</code> ({t['schedule_kind']}) | وضعیت: {state}\n"
                f"  آخرین اجرا: {t['last_status']} ({t['run_count']} بار)"
            )
        lines.append(
            "\n💡 <i>برای ساخت تسک جدید کافیست به زبان طبیعی بنویسید؛ مثلاً:</i>\n"
            "<code>هر ۳۰ ثانیه دایرکت‌های اینستاگرام رو بررسی کن و جواب بده</code>"
        )
        return "\n".join(lines)

    @dp.message(Command("start"))
    async def cmd_start(message: Message) -> None:
        uid = message.from_user.id if message.from_user else 0
        is_owner = perms.is_owner(uid)

        # Requirement 6: If Owner runs /start and AI API is not configured yet, launch step-by-step wizard
        if is_owner and not manager.model_manager.is_configured():
            wizard_states[uid] = {"wizard": "ai_setup", "step": "awaiting_base_url", "data": {}}
            await message.answer(
                "👋 <b>خوش آمدید ادمین اصلی! راه‌اندازی اولیه هوشمند آغاز شد.</b>\n\n"
                "فایل <code>token.txt</code> با موفقیت خوانده شد، اما هنوز سرویس هوش مصنوعی (AI API) تنظیم نشده است.\n\n"
                "🔹 <b>مرحله ۱ از ۴ (دریافت Base URL):</b>\n"
                "لطفاً آدرس Base URL سرویس هوش مصنوعی سازگار با OpenAI را ارسال کنید:\n"
                "مثال: <code>https://api.inceptionlabs.ai/v1</code> یا <code>https://api.openai.com/v1</code>"
            )
            return

        if is_owner:
            text = await admin_ctrl.render_status_card()
        else:
            text = (
                "👋 سلام! من دستیار هوشمند مجهز به موتور جست‌وجوی زنده اینترنت هستم.\n"
                "هر سؤالی درباره تکنولوژی، اخبار روز، نسخه‌های نرم‌افزار یا پروژه‌های GitHub دارید بپرسید."
            )
        await message.answer(text, reply_markup=build_main_menu_keyboard(is_owner))

    @dp.message(Command("مدل", "ai", "models"))
    async def cmd_models(message: Message) -> None:
        uid = message.from_user.id if message.from_user else 0
        if err := _deny_if_not_owner(uid):
            await message.answer(err)
            return
        models = await manager.model_manager.list_models()
        text = await _render_models_overview()
        await message.answer(text, reply_markup=build_models_menu_keyboard(models))

    @dp.message(Command("schedule", "tasks"))
    async def cmd_schedule(message: Message) -> None:
        uid = message.from_user.id if message.from_user else 0
        if err := _deny_if_not_owner(uid):
            await message.answer(err)
            return
        tasks = await manager.scheduler.list_dynamic_tasks()
        text = await _render_tasks_overview()
        await message.answer(text, reply_markup=build_tasks_menu_keyboard(tasks))

    @dp.message(Command("packages"))
    async def cmd_packages(message: Message) -> None:
        uid = message.from_user.id if message.from_user else 0
        if err := _deny_if_not_owner(uid):
            await message.answer(err)
            return
        pending = manager.self_extender.list_pending_requests() if manager.self_extender else []
        if not pending:
            await message.answer(
                "📦 <b>مدیریت نصب پکیج‌ها و توسعه خودکار</b>\n\n"
                "در حال حاضر هیچ درخواست نصبی در انتظار تأیید نیست.\n"
                "هرگاه ایجنت برای اجرای قابلیتی به کتابخانه جدید نیازپیدا کند، مشخصات و ریسک آن را اعلام کرده و پس از ارسال کلمه <b>«تأیید»</b> توسط شما نصب را انجام می‌دهد.",
                reply_markup=build_back_to_menu_keyboard(),
            )
            return
        lines = ["📦 <b>درخواست‌های نصب پکیج در انتظار تأیید ادمین:</b>\n"]
        for req in pending:
            lines.append(
                f"• پکیج: <code>{req['pip_spec']}</code> (شناسه: <code>{req['request_id']}</code>)\n"
                f"  علت نیاز: {req['reason']}\n"
                f"  بررسی امنیتی: {req['risk_assessment']}\n"
                "  برای نصب، عبارت <code>تأیید</code> را ارسال کنید."
            )
        await message.answer("\n".join(lines), reply_markup=build_back_to_menu_keyboard())

    @dp.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        uid = message.from_user.id if message.from_user else 0
        is_owner = perms.is_owner(uid)
        text = (
            "📚 <b>راهنمای کامل Instagram Autonomous AI Agent</b>\n\n"
            "• <code>/start</code> - پنل اصلی و راه‌اندازی هوشمند اولیه\n"
            "• <code>/مدل</code> یا <code>/ai</code> - مدیریت چند API و مدل هوش مصنوعی، تست واقعی اتصال و تغییر مدل فعال\n"
            "• <code>/schedule</code> - مدیریت تسک‌های خودکار، زمان‌بندی‌شده و رویدادمحور\n"
            "• <code>/channels</code> - اتصال تعاملی حساب تلگرام (API ID/HASH) و مدیریت کانال‌های منبع\n"
            "• <code>/settings</code> - ارسال و تبدیل خودکار فایل JSON کوکی/Session اینستاگرام و Proxy\n"
            "• <code>/packages</code> - بررسی و تأیید نصب پکیج‌های موردنیاز ایجنت\n"
            "• <code>/status</code> | <code>/stats</code> | <code>/posts</code> | <code>/stories</code>\n"
            "• <code>/messages</code> | <code>/comments</code> | <code>/duplicates</code> | <code>/lowviews</code>\n"
            "• <code>/memory</code> | <code>/logs</code> | <code>/pause</code> | <code>/resume</code>"
        )
        await message.answer(text, reply_markup=build_main_menu_keyboard(is_owner))

    @dp.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        uid = message.from_user.id if message.from_user else 0
        if err := _deny_if_not_owner(uid):
            await message.answer(err)
            return
        await message.answer(await admin_ctrl.render_status_card(), reply_markup=build_back_to_menu_keyboard())

    @dp.message(Command("stats"))
    async def cmd_stats(message: Message) -> None:
        uid = message.from_user.id if message.from_user else 0
        if err := _deny_if_not_owner(uid):
            await message.answer(err)
            return
        report = await admin_ctrl.render_stats_card()
        await message.answer(report, reply_markup=build_back_to_menu_keyboard())

    @dp.message(Command("posts"))
    async def cmd_posts(message: Message) -> None:
        uid = message.from_user.id if message.from_user else 0
        if err := _deny_if_not_owner(uid):
            await message.answer(err)
            return
        res = await manager.ig_content_manager.list_recent_media(amount=8)
        if res["status"] != "ok":
            await message.answer(f"⚠️ خطا در دریافت پست‌ها: {res.get('error')}")
            return
        lines = ["🖼 <b>فهرست آخرین پست‌های منتشرشده در پیج:</b>\n"]
        for p in res["posts"]:
            lines.append(
                f"• کد: <code>{p['code']}</code> | ❤️ {p['likes']} | 💬 {p['comments']} | 👁 {p['views']}\n"
                f"  لینک: {p['url']}"
            )
        await message.answer("\n".join(lines), reply_markup=build_back_to_menu_keyboard())

    @dp.message(Command("stories"))
    async def cmd_stories(message: Message) -> None:
        uid = message.from_user.id if message.from_user else 0
        if err := _deny_if_not_owner(uid):
            await message.answer(err)
            return
        await message.answer(
            "📱 <b>مدیریت استوری‌های اینستاگرام</b>\n"
            "عکس یا ویدئوی خود را با توضیح «استوری کن» بفرستید تا از طریق Instagrapi منتشر شود.",
            reply_markup=build_back_to_menu_keyboard(),
        )

    @dp.message(Command("messages"))
    async def cmd_messages(message: Message) -> None:
        uid = message.from_user.id if message.from_user else 0
        if err := _deny_if_not_owner(uid):
            await message.answer(err)
            return
        inbox = await manager.ig_direct.fetch_inbox(amount=8)
        if inbox["status"] != "ok":
            await message.answer(f"⚠️ {inbox.get('error')}")
            return
        lines = ["✉️ <b>آخرین پیام‌های دریافتی دایرکت:</b>\n"]
        for t in inbox["threads"]:
            users_str = ", ".join(t["users"]) or "کاربر"
            lines.append(
                f"• <b>@{users_str}</b> [{t['classification']}]\n"
                f"  پیام: <i>{t['last_message_text'][:90]}</i>\n"
                f"  Thread ID: <code>{t['thread_id']}</code>"
            )
        await message.answer("\n".join(lines), reply_markup=build_back_to_menu_keyboard())

    @dp.message(Command("comments"))
    async def cmd_comments(message: Message) -> None:
        uid = message.from_user.id if message.from_user else 0
        if err := _deny_if_not_owner(uid):
            await message.answer(err)
            return
        await message.answer(
            "💬 <b>مدیریت کامنت‌ها</b>\n"
            "ایجنت کامنت‌ها را بدون ارسال پاسخ تکراری مدیریت می‌کند. می‌توانید تسک بررسی خودکار کامنت بسازید.",
            reply_markup=build_back_to_menu_keyboard(),
        )

    @dp.message(Command("publish"))
    async def cmd_publish(message: Message) -> None:
        uid = message.from_user.id if message.from_user else 0
        if err := _deny_if_not_owner(uid):
            await message.answer(err)
            return
        await message.answer(
            "🚀 <b>انتشار محتوا</b>\n"
            "فایل عکس یا ویدئو را مستقیم بفرستید یا کانال منبع تلگرام معرفی کنید.",
            reply_markup=build_back_to_menu_keyboard(),
        )

    @dp.message(Command("sources", "channels"))
    async def cmd_channels(message: Message) -> None:
        uid = message.from_user.id if message.from_user else 0
        if err := _deny_if_not_owner(uid):
            await message.answer(err)
            return
        channels = await manager.lt_memory.get_source_channels()
        tg_status = "متصل ✅" if (manager.user_connector and manager.user_connector.is_connected) else "متصل نیست ⚠️"
        lines = [
            f"📢 <b>مدیریت حساب کاربری تلگرام (Telethon) و کانال‌های منبع</b>\n"
            f"وضعیت حساب کاربری تلگرام: <b>{tg_status}</b>\n"
        ]
        if channels:
            for ch in channels:
                mode = "انتشار خودکار" if ch["auto_publish"] else "پس از تأیید ادمین"
                lines.append(f"• <code>@{ch['channel_username']}</code> — {mode}")
        else:
            lines.append("• هنوز کانالی ثبت نشده است.")
        lines.append(
            "\nبرای اتصال حساب تلگرام با API ID و API HASH از دکمه «📱 اتصال حساب تلگرام» در منوی اصلی استفاده کنید."
        )
        await message.answer("\n".join(lines), reply_markup=build_back_to_menu_keyboard())

    @dp.message(Command("duplicates"))
    async def cmd_duplicates(message: Message) -> None:
        uid = message.from_user.id if message.from_user else 0
        if err := _deny_if_not_owner(uid):
            await message.answer(err)
            return
        report = await manager.ig_content_manager.find_duplicate_posts(amount=20)
        if report.get("status") != "نیازمند تأیید":
            await message.answer(f"⚠️ {report.get('error', 'خطا در بررسی پست‌های تکراری')}")
            return
        exact = report["exact_duplicates"]
        if not exact:
            await message.answer("✅ هیچ پست تکراری در پیج یافت نشد.")
            return
        remove_pks = [item["remove_pk"] for item in exact]
        lines = ["🔍 <b>پست‌های تکراری شناسایی‌شده با pHash:</b>\n"]
        for item in exact:
            lines.append(f"• نگهداری: {item['keep_url']}\n  حذف پیشنهادی (PK {item['remove_pk']}): {item['remove_url']}")
        await message.answer(
            "\n".join(lines),
            reply_markup=build_confirmation_keyboard("del_dups", ",".join(remove_pks[:5])),
        )

    @dp.message(Command("lowviews"))
    async def cmd_lowviews(message: Message) -> None:
        uid = message.from_user.id if message.from_user else 0
        if err := _deny_if_not_owner(uid):
            await message.answer(err)
            return
        res = await manager.ig_content_manager.find_low_performing_posts(min_views=1000, min_age_days=30)
        if res.get("status") != "نیازمند تأیید":
            await message.answer(f"⚠️ {res.get('error')}")
            return
        cands = res["candidates"]
        if not cands:
            await message.answer("✅ هیچ پستی زیر آستانه ۱۰۰۰ بازدید یافت نشد.")
            return
        pks = [c["pk"] for c in cands]
        lines = ["📉 <b>پست‌های کم‌بازدید (نیازمند تأیید حذف):</b>\n"]
        for c in cands[:8]:
            lines.append(f"• PK <code>{c['pk']}</code> | بازدید: {c['views']} | {c['url']}")
        await message.answer(
            "\n".join(lines),
            reply_markup=build_confirmation_keyboard("del_lowviews", ",".join(pks[:5])),
        )

    @dp.message(Command("memory"))
    async def cmd_memory(message: Message) -> None:
        uid = message.from_user.id if message.from_user else 0
        if err := _deny_if_not_owner(uid):
            await message.answer(err)
            return
        prefs = await manager.lt_memory.list_preferences()
        lines = ["🧠 <b>حافظه دائمی SQLite:</b>\n"]
        for p in prefs[:15]:
            if "api_hash" not in p["key"]:
                lines.append(f"• <code>{p['key']}</code>: {p['value']}")
        await message.answer("\n".join(lines), reply_markup=build_back_to_menu_keyboard())

    @dp.message(Command("settings"))
    async def cmd_settings(message: Message) -> None:
        uid = message.from_user.id if message.from_user else 0
        if err := _deny_if_not_owner(uid):
            await message.answer(err)
            return
        await message.answer(
            "⚙️ <b>تنظیمات Session اینستاگرام و Proxy</b>\n\n"
            f"• مسیر Session فعال: <code>{manager.ig_client.session_manager.session_path}</code>\n"
            f"• وضعیت اتصال: <code>{'Connected' if manager.ig_client.is_connected else 'Disconnected'}</code>\n"
            f"• پروکسی: <code>{manager.ig_client.proxy_manager.get_masked_proxy()}</code>\n\n"
            "💡 شما می‌توانید فایل <code>.json</code> حاوی کوکی مرورگر یا Session اینستاگرام را مستقیماً در همین چت ارسال کنید تا ایجنت ساختار آن را بررسی، تبدیل، تست و ذخیره کند.",
            reply_markup=build_back_to_menu_keyboard(),
        )

    @dp.message(Command("logs"))
    async def cmd_logs(message: Message) -> None:
        uid = message.from_user.id if message.from_user else 0
        if err := _deny_if_not_owner(uid):
            await message.answer(err)
            return
        await message.answer(admin_ctrl.read_recent_logs(), reply_markup=build_back_to_menu_keyboard())

    @dp.message(Command("pause"))
    async def cmd_pause(message: Message) -> None:
        uid = message.from_user.id if message.from_user else 0
        if err := _deny_if_not_owner(uid):
            await message.answer(err)
            return
        manager.agent.is_paused = True
        await message.answer("⏸ تمام فعالیت‌های خودکار ایجنت متوقف شدند.")

    @dp.message(Command("resume"))
    async def cmd_resume(message: Message) -> None:
        uid = message.from_user.id if message.from_user else 0
        if err := _deny_if_not_owner(uid):
            await message.answer(err)
            return
        manager.agent.is_paused = False
        await message.answer("▶️ فعالیت‌های خودمختار ایجنت مجدداً فعال شدند.")

    # Document Handler: Direct upload of Instagram Session / Cookie JSON file via Telegram
    @dp.message(F.document)
    async def handle_uploaded_session_document(message: Message) -> None:
        uid = message.from_user.id if message.from_user else 0
        if err := _deny_if_not_owner(uid):
            await message.answer(err)
            return

        doc = message.document
        if not doc:
            return
        file_name = (doc.file_name or "").lower()

        # Telethon .session backup restore: save the file and reconnect.
        # This is how a login survives a redeploy (which wipes the disk).
        if file_name.endswith(".session"):
            if not manager.bot:
                return
            buf = BytesIO()
            await manager.bot.download(doc, destination=buf)
            session_path = Path(settings.TG_SESSION_PATH)
            session_path.parent.mkdir(parents=True, exist_ok=True)
            session_path.write_bytes(buf.getvalue())
            if manager.user_connector:
                await manager.user_connector.start()
                if manager.user_connector.is_connected:
                    await message.answer("✅ <b>Session تلگرام بازیابی شد و حساب متصل است!</b>")
                else:
                    await message.answer(
                        "⚠️ فایل Session ذخیره شد اما حساب هنوز متصل نیست. "
                        "ممکن است Session منقضی شده باشد؛ با ارسال شماره تلفن دوباره لاگین کنید."
                    )
            return

        # Media sent as a file (a video, audio or image as a document):
        # download it and let the agent work on it instead of demanding
        # a .json file. Session (.session) and .json files are handled above.
        media = _pick_message_media(message)
        if media is not None:
            media_obj, kind, suffix = media
            if not manager.bot:
                return
            media_dir = Path(settings.TEMP_MEDIA_DIR)
            media_dir.mkdir(parents=True, exist_ok=True)
            media_path = media_dir / f"doc_{kind}_{uid}_{message.message_id}{suffix}"
            await manager.bot.download(media_obj, destination=media_path)
            caption = (message.caption or "").strip()
            user_text = caption or (
                "آهنگش را پیدا کن و برایم بفرست." if kind in ("audio", "voice") else "این فایل را پردازش کن."
            )
            prompt = _media_prompt(kind, media_path, user_text)

            async def _run_doc_media(update_progress: Any) -> str:
                await update_progress("<b>در حال پردازش فایل… ⏳</b>")
                result = await manager.agent.process_command(user_id=uid, user_message=prompt, channel="telegram")
                return result.get("reply", "")

            await manager.rich_responder.run_with_thinking_stream(
                chat_id=message.chat.id,
                coro_factory=_run_doc_media,
            )
            return

        if not file_name.endswith(".json"):
            await message.answer("⚠️ لطفاً یک فایل با پسوند <code>.json</code> حاوی Session یا Cookie اینستاگرام ارسال کنید.")
            return

        if not manager.bot:
            return

        buf = BytesIO()
        await manager.bot.download(doc, destination=buf)
        raw_json = buf.getvalue().decode("utf-8", errors="replace")

        res = await manager.ig_client.import_and_verify_session_json(raw_json)
        if res.get("status") == "invalid_format":
            await message.answer(
                "❌ <b>فرمت فایل JSON ارسالی قابل استفاده نیست</b>\n\n"
                f"• نوع تشخیص‌داده‌شده: <code>{res.get('format')}</code>\n"
                f"• علت: {res.get('error')}\n"
                f"• راهنما: {res.get('hint')}"
            )
            return

        if res.get("status") == "connected":
            await message.answer(
                "✅ <b>اتصال اینستاگرام با موفقیت برقرار و Session ذخیره شد!</b>\n\n"
                f"• فرمت ورودی: <code>{res.get('detected_format')}</code>\n"
                f"• اکانت متصل: <code>@{res.get('username')}</code>\n"
                f"• توضیح تبدیل: {res.get('conversion_note')}"
            )
        else:
            await message.answer(
                "⚠️ <b>ساختار JSON تبدیل شد اما تست اتصال به اینستاگرام ناموفق بود:</b>\n"
                f"• وضعیت: <code>{res.get('status')}</code>\n"
                f"• خطا: {res.get('error')}"
            )

    # Voice messages: download the voice note and let the agent identify
    # the song in it (identify_song tool), then reply with the result.
    @dp.message(F.voice)
    async def handle_voice_message(message: Message) -> None:
        uid = message.from_user.id if message.from_user else 0
        if err := _deny_if_not_owner(uid):
            await message.answer(err)
            return
        if not message.voice or not manager.bot:
            return
        media_dir = Path(settings.TEMP_MEDIA_DIR)
        media_dir.mkdir(parents=True, exist_ok=True)
        voice_path = media_dir / f"voice_{uid}_{message.message_id}.ogg"
        await manager.bot.download(message.voice, destination=voice_path)
        prompt = (
            "یک پیام صوتی فرستادم و در این مسیر ذخیره شد: "
            f"{voice_path} — آهنگ داخل آن را پیدا کن و برایم بفرست."
        )

        async def _run_voice(update_progress: Any) -> str:
            await update_progress("<b>در حال گوش دادن به پیام صوتی… 🎵</b>")
            result = await manager.agent.process_command(user_id=uid, user_message=prompt, channel="telegram")
            return result.get("reply", "")

        await manager.rich_responder.run_with_thinking_stream(
            chat_id=message.chat.id,
            coro_factory=_run_voice,
        )

    # Audio files (music): download and let the agent identify the song
    # (identify_song tool). Without this handler an audio message matched
    # no handler at all and was silently ignored.
    @dp.message(F.audio)
    async def handle_audio_message(message: Message) -> None:
        uid = message.from_user.id if message.from_user else 0
        if err := _deny_if_not_owner(uid):
            await message.answer(err)
            return
        if not message.audio or not manager.bot:
            return
        media_dir = Path(settings.TEMP_MEDIA_DIR)
        media_dir.mkdir(parents=True, exist_ok=True)
        audio_path = media_dir / f"audio_{uid}_{message.message_id}.mp3"
        await manager.bot.download(message.audio, destination=audio_path)
        user_text = (message.caption or "").strip() or "آهنگش را پیدا کن و برایم بفرست."
        prompt = _media_prompt("audio", audio_path, user_text)

        async def _run_audio(update_progress: Any) -> str:
            await update_progress("<b>در حال گوش دادن به فایل صوتی… 🎵</b>")
            result = await manager.agent.process_command(user_id=uid, user_message=prompt, channel="telegram")
            return result.get("reply", "")

        await manager.rich_responder.run_with_thinking_stream(
            chat_id=message.chat.id,
            coro_factory=_run_audio,
        )

    # Videos (and round video notes): download and let the agent work on the
    # file itself — find its song, download it, or describe it.
    @dp.message(F.video | F.video_note)
    async def handle_video_message(message: Message) -> None:
        uid = message.from_user.id if message.from_user else 0
        if err := _deny_if_not_owner(uid):
            await message.answer(err)
            return
        media_obj = message.video or message.video_note
        if not media_obj or not manager.bot:
            return
        media_dir = Path(settings.TEMP_MEDIA_DIR)
        media_dir.mkdir(parents=True, exist_ok=True)
        video_path = media_dir / f"video_{uid}_{message.message_id}.mp4"
        await manager.bot.download(media_obj, destination=video_path)
        user_text = (message.caption or "").strip() or "این ویدئو را پردازش کن."
        prompt = _media_prompt("video", video_path, user_text)

        async def _run_video(update_progress: Any) -> str:
            await update_progress("<b>در حال پردازش ویدئو… 🎬</b>")
            result = await manager.agent.process_command(user_id=uid, user_message=prompt, channel="telegram")
            return result.get("reply", "")

        await manager.rich_responder.run_with_thinking_stream(
            chat_id=message.chat.id,
            coro_factory=_run_video,
        )

    # Photos: download the photo and let the agent describe what is in it
    # (describe_image tool). A caption, if sent with the photo, is the question.
    @dp.message(F.photo)
    async def handle_photo_message(message: Message) -> None:
        uid = message.from_user.id if message.from_user else 0
        if err := _deny_if_not_owner(uid):
            await message.answer(err)
            return
        if not message.photo or not manager.bot:
            return
        largest = message.photo[-1]
        media_dir = Path(settings.TEMP_MEDIA_DIR)
        media_dir.mkdir(parents=True, exist_ok=True)
        photo_path = media_dir / f"photo_{uid}_{message.message_id}.jpg"
        await manager.bot.download(largest, destination=photo_path)
        question = (message.caption or "").strip() or "تو این عکس چی هست؟ توصیفش کن."
        prompt = f"این عکس را ببین و توصیف کن: {photo_path} — سوال من: {question}"

        async def _run_photo(update_progress: Any) -> str:
            await update_progress("<b>در حال دیدن عکس… 👀</b>")
            result = await manager.agent.process_command(user_id=uid, user_message=prompt, channel="telegram")
            return result.get("reply", "")

        await manager.rich_responder.run_with_thinking_stream(
            chat_id=message.chat.id,
            coro_factory=_run_photo,
        )

    # Interactive Callback Queries for /مدل, Task Engine, Wizards, and Confirmations
    @dp.callback_query()
    async def handle_all_callbacks(callback: CallbackQuery) -> None:
        uid = callback.from_user.id if callback.from_user else 0
        data = callback.data or ""

        if data == "cmd_start":
            is_owner = perms.is_owner(uid)
            text = await admin_ctrl.render_status_card() if is_owner else "منوی اصلی:"
            if callback.message:
                await callback.message.edit_text(text, reply_markup=build_main_menu_keyboard(is_owner))
            await callback.answer()
            return

        if err := _deny_if_not_owner(uid):
            await callback.answer(err[:200], show_alert=True)
            return

        if data == "cmd_models_menu":
            models = await manager.model_manager.list_models()
            if callback.message:
                await callback.message.edit_text(
                    await _render_models_overview(),
                    reply_markup=build_models_menu_keyboard(models),
                )
            await callback.answer()
            return

        if data == "wiz_add_model":
            wizard_states[uid] = {"wizard": "ai_setup", "step": "awaiting_base_url", "data": {}}
            if callback.message:
                await callback.message.answer(
                    "🤖 <b>افزودن سرویس و مدل جدید هوش مصنوعی</b>\n\n"
                    "🔹 <b>مرحله ۱ از ۴:</b> لطفاً آدرس <code>Base URL</code> را وارد کنید:\n"
                    "مثال: <code>https://api.inceptionlabs.ai/v1</code>"
                )
            await callback.answer()
            return

        if data.startswith("mdl_select:"):
            model_id = data.split(":", 1)[1]
            await manager.model_manager.switch_active_model(model_id)
            models = await manager.model_manager.list_models()
            if callback.message:
                await callback.message.edit_text(
                    await _render_models_overview(),
                    reply_markup=build_models_menu_keyboard(models),
                )
            await callback.answer("مدل فعال کل پروژه تغییر کرد ✅")
            return

        if data.startswith("mdl_retest:"):
            model_id = data.split(":", 1)[1]
            res = await manager.model_manager.retest_saved_model(model_id)
            models = await manager.model_manager.list_models()
            if callback.message:
                await callback.message.edit_text(
                    await _render_models_overview(),
                    reply_markup=build_models_menu_keyboard(models),
                )
            await callback.answer("تست اتصال موفق بود ✅" if res.get("ok") else f"خطا: {res.get('error')}", show_alert=True)
            return

        if data.startswith("mdl_delete:"):
            model_id = data.split(":", 1)[1]
            await manager.model_manager.delete_model(model_id)
            models = await manager.model_manager.list_models()
            if callback.message:
                await callback.message.edit_text(
                    await _render_models_overview(),
                    reply_markup=build_models_menu_keyboard(models),
                )
            await callback.answer("مدل حذف شد")
            return

        if data == "cmd_schedule":
            tasks = await manager.scheduler.list_dynamic_tasks()
            if callback.message:
                await callback.message.edit_text(
                    await _render_tasks_overview(),
                    reply_markup=build_tasks_menu_keyboard(tasks),
                )
            await callback.answer()
            return

        if data.startswith("tsk_approve:"):
            tid = data.split(":", 1)[1]
            await manager.scheduler.approve_and_start_task(tid)
            tasks = await manager.scheduler.list_dynamic_tasks()
            if callback.message:
                await callback.message.edit_text(
                    await _render_tasks_overview(),
                    reply_markup=build_tasks_menu_keyboard(tasks),
                )
            await callback.answer("تسک تأیید و فعال شد ✅")
            return

        if data.startswith("tsk_pause:"):
            tid = data.split(":", 1)[1]
            await manager.scheduler.pause_task(tid)
            tasks = await manager.scheduler.list_dynamic_tasks()
            if callback.message:
                await callback.message.edit_text(
                    await _render_tasks_overview(),
                    reply_markup=build_tasks_menu_keyboard(tasks),
                )
            await callback.answer("تسک متوقف شد ⏸")
            return

        if data.startswith("tsk_resume:"):
            tid = data.split(":", 1)[1]
            await manager.scheduler.resume_task(tid)
            tasks = await manager.scheduler.list_dynamic_tasks()
            if callback.message:
                await callback.message.edit_text(
                    await _render_tasks_overview(),
                    reply_markup=build_tasks_menu_keyboard(tasks),
                )
            await callback.answer("تسک فعال شد ▶️")
            return

        if data.startswith("tsk_delete:"):
            tid = data.split(":", 1)[1]
            await manager.scheduler.delete_task(tid)
            tasks = await manager.scheduler.list_dynamic_tasks()
            if callback.message:
                await callback.message.edit_text(
                    await _render_tasks_overview(),
                    reply_markup=build_tasks_menu_keyboard(tasks),
                )
            await callback.answer("تسک حذف شد 🗑")
            return

        if data == "wiz_ig_session":
            wizard_states[uid] = {"wizard": "ig_session", "step": "awaiting_json"}
            if callback.message:
                await callback.message.answer(
                    "🔐 <b>ارسال مستقیم Session یا Cookie اینستاگرام</b>\n\n"
                    "یک فایل <code>.json</code> یا متن JSON حاوی کوکی‌های مرورگر (شامل <code>sessionid</code>) یا خروجی Instagrapi را ارسال کنید تا ایجنت فرمت آن را تشخیص داده، تبدیل و تست کند."
                )
            await callback.answer()
            return

        if data == "wiz_tg_login":
            wizard_states[uid] = {"wizard": "tg_login", "step": "awaiting_api_id", "data": {}}
            if callback.message:
                await callback.message.answer(
                    "📱 <b>اتصال تعاملی حساب کاربری تلگرام (Telethon)</b>\n\n"
                    "🔹 <b>مرحله ۱ از ۵:</b> لطفاً عدد <code>API ID</code> دریافتی از my.telegram.org را ارسال کنید:"
                )
            await callback.answer()
            return

        if data.startswith("confirm_"):
            _, payload = data.split(":", 1)
            pks = [p for p in payload.split(",") if p]
            res = await manager.ig_content_manager.delete_posts(media_pks=pks, confirmed=True)
            if callback.message:
                await callback.message.edit_text(
                    f"🗑 <b>نتیجه حذف پس از تأیید ادمین:</b>\nوضعیت: <b>{res['status']}</b>"
                )
            await callback.answer("انجام شد")
            return

        await callback.answer()

    # Main Natural Language & Interactive Wizard State Machine Handler
    @dp.message(F.text)
    async def handle_text_and_wizards(message: Message) -> None:
        uid = message.from_user.id if message.from_user else 0
        chat_id = message.chat.id
        text = (message.text or "").strip()

        # 1. Check if Owner typed «تأیید» for a pending package installation
        if perms.is_owner(uid) and text in ("تأیید", "تایید") and manager.self_extender:
            pending_pkgs = manager.self_extender.list_pending_requests()
            if pending_pkgs:
                req = pending_pkgs[0]
                res = manager.self_extender.approve_and_install_package(req["request_id"], confirmed_by_owner=True)
                await message.answer(
                    f"📦 <b>نتیجه نصب پکیج <code>{req['pip_spec']}</code>:</b>\n"
                    f"• وضعیت: <b>{res['status']}</b>\n"
                    f"• تست Import ماژول: <code>{res.get('import_verified')}</code>"
                )
                return

        # 2. Check active interactive wizards (AI Setup, Telethon Login, Instagram Session JSON paste)
        if uid in wizard_states and perms.is_owner(uid):
            state = wizard_states[uid]
            wiz = state["wizard"]
            step = state["step"]

            if text in ("لغو", "/cancel"):
                wizard_states.pop(uid, None)
                await message.answer("❌ فرایند راه‌اندازی لغو شد.")
                return

            # Wizard A: 4-step AI API Onboarding & Live Verification
            if wiz == "ai_setup":
                if step == "awaiting_base_url":
                    check = manager.model_manager.validate_base_url(text)
                    if not check["valid"]:
                        await message.answer(f"⚠️ {check['error']}\nدوباره آدرس Base URL را ارسال کنید:")
                        return
                    state["data"]["base_url"] = check["normalized_url"]
                    state["step"] = "awaiting_api_key"
                    await message.answer(
                        f"✅ آدرس <code>{check['normalized_url']}</code> معتبر است.\n\n"
                        "🔹 <b>مرحله ۲ از ۴ (دریافت API Key):</b>\n"
                        "لطفاً کلید API (<code>API Key</code>) خود را ارسال کنید (به صورت رمزنگاری‌شده ذخیره می‌شود):"
                    )
                    return

                if step == "awaiting_api_key":
                    state["data"]["api_key"] = text
                    state["step"] = "awaiting_model_name"
                    await message.answer(
                        "🔐 کلید API دریافت شد.\n\n"
                        "🔹 <b>مرحله ۳ از ۴ (دریافت نام مدل):</b>\n"
                        "لطفاً نام دقیق مدل (مثلاً <code>gpt-4o-mini</code> یا <code>mercury-coder-small</code>) را ارسال کنید:"
                    )
                    return

                if step == "awaiting_model_name":
                    model_name = text
                    base_url = state["data"]["base_url"]
                    api_key = state["data"]["api_key"]
                    await message.answer(
                        "⏳ <b>مرحله ۴ از ۴: در حال ارسال درخواست واقعی و تست اتصال به API...</b>"
                    )
                    reg_res = await manager.model_manager.register_verified_model(
                        base_url=base_url,
                        api_key=api_key,
                        model_name=model_name,
                        set_active=True,
                        skip_live_test=False,
                    )
                    wizard_states.pop(uid, None)
                    if reg_res.get("ok"):
                        await message.answer(
                            "✅ <b>تست اتصال واقعی موفقیت‌آمیز بود و مدل به عنوان مدل فعال کل پروژه ذخیره شد!</b>\n\n"
                            f"• نام مدل: <code>{reg_res['model_name']}</code>\n"
                            f"• آدرس سرویس: <code>{reg_res['base_url']}</code>\n"
                            "برای مدیریت یا افزودن مدل‌های دیگر می‌توانید از دستور <code>/مدل</code> استفاده کنید.",
                            reply_markup=build_main_menu_keyboard(True),
                        )
                    else:
                        await message.answer(
                            "❌ <b>تست اتصال به API ناموفق بود (مدل ثبت نشد):</b>\n"
                            f"علت خطا: <code>{reg_res.get('error')}</code>\n\n"
                            "برای تلاش مجدد دستور <code>/مدل</code> یا <code>/start</code> را ارسال کنید."
                        )
                    return

            # Wizard B: Raw JSON text for Instagram Session
            if wiz == "ig_session" and step == "awaiting_json":
                wizard_states.pop(uid, None)
                res = await manager.ig_client.import_and_verify_session_json(text)
                if res.get("status") == "connected":
                    await message.answer(
                        f"✅ <b>Session اینستاگرام تأیید و ذخیره شد!</b>\nاکانت: <code>@{res.get('username')}</code>"
                    )
                else:
                    await message.answer(f"❌ <b>خطا در بررسی Session:</b>\n{res.get('error')}")
                return

            # Wizard C: 5-step Telethon User Account Login
            if wiz == "tg_login" and manager.user_connector:
                if step == "awaiting_api_id":
                    if not text.isdigit():
                        await message.answer("⚠️ مقدار API ID باید فقط شامل اعداد باشد. دوباره ارسال کنید:")
                        return
                    state["data"]["api_id"] = int(text)
                    state["step"] = "awaiting_api_hash"
                    await message.answer("🔹 <b>مرحله ۲ از ۵:</b> لطفاً رشته <code>API HASH</code> را ارسال کنید:")
                    return

                if step == "awaiting_api_hash":
                    state["data"]["api_hash"] = text
                    state["step"] = "awaiting_phone"
                    await message.answer(
                        "🔹 <b>مرحله ۳ از ۵:</b> لطفاً شماره تلفن حساب تلگرام را با پیش‌شماره کشور ارسال کنید (مثال: <code>+989123456789</code>):"
                    )
                    return

                if step == "awaiting_phone":
                    res = await manager.user_connector.begin_interactive_login(
                        api_id=state["data"]["api_id"],
                        api_hash=state["data"]["api_hash"],
                        phone=text,
                    )
                    if res["status"] == "code_sent":
                        state["step"] = "awaiting_code"
                        await message.answer(
                            "📩 <b>مرحله ۴ از ۵:</b> کد تأیید تلگرام ارسال شد.\n"
                            "لطفاً کد را (با فاصله یا خط تیره، مثلاً <code>1 2 3 4 5</code>) ارسال کنید:"
                        )
                    elif res["status"] == "already_authorized":
                        wizard_states.pop(uid, None)
                        await message.answer(f"✅ {res['message']}")
                    else:
                        wizard_states.pop(uid, None)
                        await message.answer(f"❌ خطا در ارسال کد: {res.get('error')}")
                    return

                if step == "awaiting_code":
                    res = await manager.user_connector.submit_verification_code(text)
                    if res["status"] == "connected":
                        wizard_states.pop(uid, None)
                        await message.answer(f"✅ {res['message']}\nحساب: <code>@{res.get('username')}</code>")
                    elif res["status"] == "two_factor_required":
                        state["step"] = "awaiting_2fa"
                        await message.answer(
                            "🔐 <b>مرحله ۵ از ۵ (تأیید دومرحله‌ای):</b>\n"
                            f"{res['message']}"
                        )
                    else:
                        await message.answer(f"❌ {res.get('error')}")
                    return

                if step == "awaiting_2fa":
                    res = await manager.user_connector.submit_two_factor_password(text)
                    wizard_states.pop(uid, None)
                    if res["status"] == "connected":
                        await message.answer(f"✅ {res['message']}")
                    else:
                        await message.answer(f"❌ {res.get('error')}")
                    return

        # 2b. Natural-language intent routing (Owner only): the AI agent has no
        # login/session/timer tools, so plain-text intents must start the matching
        # wizard directly instead of falling through to the agent, which would
        # reply "I don't have a tool for that".
        if perms.is_owner(uid):
            _fa_digits = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
            norm_text = text.translate(_fa_digits)
            clean_phone = re.sub(r"[\s\-]", "", norm_text)

            # (a-2) Delete / pause a scheduled task by chat ("اون تسک ۵ ثانیه‌ای رو حذف کن").
            if "تسک" in norm_text and ("حذف" in norm_text or "متوقف" in norm_text or "پاک" in norm_text):
                m_id = re.search(r"tsk_[a-z0-9]+", norm_text)
                target_id = m_id.group(0) if m_id else None
                if not target_id and manager.scheduler:
                    tasks = await manager.scheduler.list_dynamic_tasks()
                    active = [t for t in tasks if t.get("enabled") and t.get("approved_by_owner")]
                    dm_tasks = [t for t in active if t.get("task_type") == "auto_reply_directs"]
                    if len(dm_tasks) == 1:
                        target_id = dm_tasks[0]["task_id"]
                if target_id:
                    if "متوقف" in norm_text:
                        res = await manager.scheduler.pause_task(target_id)
                        await message.answer(f"⏸ تسک <code>{target_id}</code> متوقف شد.")
                    else:
                        await manager.scheduler.delete_task(target_id)
                        await message.answer(f"🗑 تسک <code>{target_id}</code> حذف شد و دیگر اجرا نمی‌شود.")
                else:
                    tasks = await manager.scheduler.list_dynamic_tasks() if manager.scheduler else []
                    lines = ["⏰ <b>تسک‌های فعال:</b>"] + [
                        f"• <code>{t['task_id']}</code> — {t['title']}"
                        for t in tasks if t.get("enabled")
                    ] or ["هیچ تسک فعالی نیست."]
                    await message.answer(
                        "\n".join(lines)
                        + "\n\nکدام تسک حذف شود؟ شناسه‌اش (مثلاً <code>tsk_a6ed388f</code>) را بفرستید."
                    )
                return

            # (a-3) Message me via the connected Telegram USER account (userbot),
            # not via this bot. The bot must never claim a bot-sent message came
            # from the user account — and must send the user's OWN words, not a
            # hardcoded test text: "بگو سلام خوبی" means the message IS "سلام خوبی".
            if any(k in norm_text for k in ("یوزربات", "یوزر اکانت", "اکانت تلگرام", "اون اکانت", "با اون", "به اون", "اکانت وصل")) and any(
                k in norm_text for k in ("پیام بده", "پیام بفرست", "پیام بده بهم", "بهم پیام بده")
            ):
                # Extract the exact text the user asked to send.
                custom_text = None
                m_say = re.search(r"بگو\s+(.+)$", norm_text)
                if m_say:
                    custom_text = m_say.group(1).strip().strip("\"'«»").strip()
                if not custom_text:
                    m_q = re.search(r"[\"«]([^\"»]+)[\"»]", norm_text)
                    if m_q:
                        custom_text = m_q.group(1).strip()
                if not custom_text:
                    m_col = re.search(r"پیام بده\s*:\s*(.+)$", norm_text)
                    if m_col:
                        custom_text = m_col.group(1).strip()
                send_text = custom_text or "سلام! 👋 این پیام از طریق اکانت تلگرام متصل شما (یوزربات) ارسال شد — اتصال برقرار است. ✅"
                if manager.user_connector and manager.user_connector.is_connected:
                    owner_username = message.from_user.username if message.from_user else None
                    res = await manager.user_connector.send_message_to_owner(
                        send_text,
                        username=owner_username,
                    )
                    if res.get("status") == "ok":
                        dest = res.get("sent_via", "")
                        if dest.startswith("me"):
                            await message.answer(
                                "⚠️ پیام فقط به «پیام‌های ذخیره‌شده» (Saved Messages) همان اکانت رسید، نه به چت شما — "
                                "چون یوزرنیم شما برای ارسال مستقیم پیدا نشد."
                            )
                        else:
                            await message.answer(
                                "✅ پیام آزمایشی از طریق <b>اکانت تلگرام متصل (یوزربات)</b> به چت شما ارسال شد. "
                                "اگر آن را دریافت کردید، یعنی اتصال یوزربات برقرار است."
                            )
                    else:
                        await message.answer(f"❌ خطا در ارسال پیام از طریق یوزربات: {res.get('error')}")
                else:
                    await message.answer(
                        "⚠️ حساب کاربری تلگرام (یوزربات) هنوز متصل نیست. "
                        "اول با ارسال شماره تلفن لاگین کنید، بعد این تست را انجام می‌دهم."
                    )
                return

            # (a-4) Which Telegram account is the userbot session logged into?
            if any(k in norm_text for k in ("وضعیت اتصال تلگرام", "اکانت یوزربات", "یوزربات وصل", "به کدوم اکانت وصل")):
                st = await manager.user_connector.get_account_status() if manager.user_connector else {"status": "not_connected"}
                if st.get("status") == "ok":
                    await message.answer(
                        "📱 <b>وضعیت یوزربات:</b> متصل ✅\n"
                        f"اکانت متصل: @{st.get('username') or '؟'} (آیدی: <code>{st.get('user_id')}</code>)"
                    )
                else:
                    await message.answer(f"⚠️ یوزربات متصل نیست: {st.get('error', '')}")
                return

            # (a0) A Telethon login is already in progress: a digits-only message
            # is the login CODE (while _pending_phone is set) or the 2FA PASSWORD
            # (while _awaiting_2fa is set) — never a new phone number. This is
            # what broke the 2FA step: "30556699" was misread as a phone number
            # and sent to send_code_request, which rejected it as invalid.
            if manager.user_connector:
                if getattr(manager.user_connector, "_awaiting_2fa", False):
                    res = await manager.user_connector.submit_two_factor_password(text)
                    if res["status"] == "connected":
                        await message.answer(f"✅ {res['message']}")
                    else:
                        await message.answer(f"❌ {res.get('error')}")
                    return
                if getattr(manager.user_connector, "_pending_phone", None):
                    digits_only = re.sub(r"[\s\-]", "", norm_text)
                    if re.fullmatch(r"\d{5,6}", digits_only):
                        res = await manager.user_connector.submit_verification_code(text)
                        if res["status"] == "connected":
                            await message.answer(f"✅ {res['message']}\nحساب: <code>@{res.get('username')}</code>")
                        elif res["status"] == "two_factor_required":
                            await message.answer(
                                "🔐 <b>تأیید دومرحله‌ای (Two-Step Verification) روی این حساب فعال است.</b>\n"
                                "لطفاً رمز عبور دومرحله‌ای را ارسال کنید:"
                            )
                        else:
                            await message.answer(f"❌ {res.get('error')}")
                        return
                    if digits_only.isdigit():
                        await message.answer(
                            "⚠️ کد تأیید تلگرام ۵ رقمی است. لطفاً کد ۵ رقمی‌ای که تلگرام فرستاد را ارسال کنید:"
                        )
                        return

            # (a) Phone number -> Telethon login. Use saved/env TG_API_ID &
            # TG_API_HASH when present, so the owner only sends the phone.
            # Minimum 10 digits: shorter numbers are login codes / 2FA passwords.
            if re.fullmatch(r"\+?\d{10,15}", clean_phone):
                if manager.user_connector:
                    creds = await manager.user_connector.get_saved_credentials()
                    if creds.get("api_id") and creds.get("api_hash"):
                        res = await manager.user_connector.begin_interactive_login(
                            api_id=int(creds["api_id"]),
                            api_hash=str(creds["api_hash"]),
                            phone=clean_phone,
                        )
                        if res["status"] == "code_sent":
                            wizard_states[uid] = {"wizard": "tg_login", "step": "awaiting_code", "data": {}}
                            await message.answer(
                                "📩 <b>کد تأیید تلگرام ارسال شد.</b>\n"
                                "لطفاً کد را (مثلاً <code>1 2 3 4 5</code>) ارسال کنید:"
                            )
                        elif res["status"] == "already_authorized":
                            await message.answer(f"✅ {res['message']}")
                        else:
                            await message.answer(f"❌ خطا در ارسال کد: {res.get('error')}")
                    else:
                        wizard_states[uid] = {"wizard": "tg_login", "step": "awaiting_api_id", "data": {}}
                        await message.answer(
                            "📱 <b>اتصال حساب تلگرام (Telethon)</b>\n\n"
                            "🔹 <b>مرحله ۱ از ۵:</b> لطفاً عدد <code>API ID</code> را ارسال کنید:"
                        )
                else:
                    await message.answer("❌ اتصال حساب تلگرام در این نسخه فعال نیست.")
                return

            # (b) Pasted Instagram session / cookies JSON -> import & verify.
            stripped = text.strip()
            if stripped.startswith(("[", "{")) and any(
                k in stripped for k in ("sessionid", "csrftoken", "ds_user_id", "cookies", "uuids", "authorization_data")
            ):
                res = await manager.ig_client.import_and_verify_session_json(text)
                if res.get("status") == "connected":
                    await message.answer(
                        "✅ <b>اتصال اینستاگرام با موفقیت برقرار و Session ذخیره شد!</b>\n\n"
                        f"• فرمت ورودی: <code>{res.get('detected_format')}</code>\n"
                        f"• اکانت متصل: <code>@{res.get('username')}</code>"
                    )
                elif res.get("status") == "invalid_format":
                    await message.answer(
                        "❌ <b>فرمت Session ارسالی قابل استفاده نیست</b>\n\n"
                        f"• علت: {res.get('error')}\n"
                        f"• راهنما: {res.get('hint')}"
                    )
                else:
                    await message.answer(f"❌ <b>خطا در بررسی Session:</b>\n{res.get('error')}")
                return

            # (c) One-off reminder: "بعد از ۳۰ ثانیه بهم پیام بده" / "in 30 seconds message me"
            rem = re.search(
                r"(?:بعد از|after|in)\s*(\d+)\s*(ثانیه|دقیقه|ساعت|seconds?|minutes?|hours?)",
                norm_text,
                re.IGNORECASE,
            )
            if rem and any(k in norm_text for k in ("پیام", "یادآوری", "message", "remind", "یادم")):
                amount = int(rem.group(1))
                unit = rem.group(2)
                delay = amount * (1 if unit.startswith(("ثانیه", "second")) else 60 if unit.startswith(("دقیقه", "minute")) else 3600)
                delay = max(5, min(delay, 24 * 3600))
                reminder_text = (
                    f"⏰ <b>یادآوری درخواستی شما</b>\n"
                    f"گفتید بعد از {amount} {unit} به شما پیام بدهم — این همان پیام است. ✅"
                )
                res = await manager.scheduler.create_one_off_reminder(chat_id, delay, reminder_text)
                if res.get("ok"):
                    await message.answer(
                        f"⏰ <b>یادآوری تنظیم شد.</b>\nبعد از {amount} {unit} همین‌جا به شما پیام می‌دهم."
                    )
                else:
                    await message.answer(f"❌ نتوانستم یادآوری را تنظیم کنم: {res.get('error')}")
                return

        # 2b. Reply-to-media: if the user replied to a message carrying a
        # video, audio, voice note or photo, download THAT file and tell the
        # agent its path, so "استخراجش کن" / "آهنگش را پیدا کن" works on the
        # replied-to file instead of asking the user to send it again.
        if message.reply_to_message is not None and manager.bot:
            replied = message.reply_to_message
            media = _pick_message_media(replied)
            if media is not None:
                media_obj, kind, suffix = media
                media_dir = Path(settings.TEMP_MEDIA_DIR)
                media_dir.mkdir(parents=True, exist_ok=True)
                media_path = media_dir / f"reply_{kind}_{uid}_{replied.message_id}{suffix}"
                await manager.bot.download(media_obj, destination=media_path)
                text = _media_prompt(kind, media_path, text, replied=True)
            else:
                quoted = (replied.text or replied.caption or "").strip()
                if quoted:
                    text = f"کاربر روی این پیام ریپلای کرده: «{quoted}»\n\nدستور کاربر: {text}"

        # 3. Standard Natural Language Processing using Telegram Rich Message Draft (<tg-thinking>Thinking…</tg-thinking>)
        # Progress labels per tool, so a long download/search shows what is
        # happening instead of one frozen "thinking" line for minutes.
        _STEP_LABELS = {
            "download_media": "<b>در حال دانلود… ⬇️</b>",
            "identify_song": "<b>در حال شناسایی آهنگ… 🎵</b>",
            "find_song_of_video": "<b>در حال پیدا کردن آهنگ ویدیو… 🎵</b>",
            "describe_image": "<b>در حال دیدن عکس… 👀</b>",
            "describe_video": "<b>در حال دیدن ویدیو… 👀</b>",
            "web_search": "<b>در حال جستجو… 🔍</b>",
        }

        async def _execute_with_draft_updates(update_progress: Any) -> str:
            await update_progress("<b>در حال تحلیل دستور و انتخاب ابزار… ✨</b>")

            async def _on_agent_step(step_idx: int, tool_name: str, phase: str) -> None:
                if phase == "start":
                    label = _STEP_LABELS.get(tool_name, f"<b>در حال اجرای {tool_name}…</b>")
                    await update_progress(label)
                else:
                    await update_progress("<b>در حال تحلیل نتیجه… ✨</b>")

            result = await manager.agent.process_command(
                user_id=uid, user_message=text, channel="telegram", on_step=_on_agent_step,
            )

            trace_lines = []
            for step_item in result.get("trace", []):
                trace_lines.append(f"• گام {step_item['step']}: <code>{step_item['tool']}</code> ({step_item['status']})")
            # Short conversational answers stay short: no tool trace and no
            # status footer for them — the trace is only useful for real tasks.
            if trace_lines and len(result.get("reply", "")) > 200:
                trace_block = "\n\n🛠 <b>مراحل اجرای ابزارها:</b>\n" + "\n".join(trace_lines)
                return f"{result['reply']}{trace_block}\n\nوضعیت نهایی: <b>{result['status']}</b>"
            return result["reply"]

        await manager.rich_responder.run_with_thinking_stream(
            chat_id=chat_id,
            coro_factory=_execute_with_draft_updates,
        )
