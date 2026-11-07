from io import BytesIO
import re
from typing import Any, Dict
from aiogram import Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from core.permissions import PermissionManager
from telegram.keyboards import (
    build_main_menu_keyboard,
    build_back_to_menu_keyboard,
    build_confirmation_keyboard,
    build_models_menu_keyboard,
    build_tasks_menu_keyboard,
)
from telegram.admin import AdminCommandController


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
        if not doc or not (doc.file_name or "").lower().endswith(".json"):
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

            # (a) Phone number -> Telethon login. Use saved/env TG_API_ID &
            # TG_API_HASH when present, so the owner only sends the phone.
            if re.fullmatch(r"\+?\d{8,15}", clean_phone):
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

        # 3. Standard Natural Language Processing using Telegram Rich Message Draft (<tg-thinking>Thinking…</tg-thinking>)
        async def _execute_with_draft_updates(update_progress: Any) -> str:
            await update_progress("<b>در حال تحلیل دستور و انتخاب ابزار… ✨</b>")
            result = await manager.agent.process_command(user_id=uid, user_message=text, channel="telegram")

            trace_lines = []
            for step_item in result.get("trace", []):
                trace_lines.append(f"• گام {step_item['step']}: <code>{step_item['tool']}</code> ({step_item['status']})")
            trace_block = ("\n\n🛠 <b>مراحل اجرای ابزارها:</b>\n" + "\n".join(trace_lines)) if trace_lines else ""

            return f"{result['reply']}{trace_block}\n\nوضعیت نهایی: <b>{result['status']}</b>"

        await manager.rich_responder.run_with_thinking_stream(
            chat_id=chat_id,
            coro_factory=_execute_with_draft_updates,
        )
