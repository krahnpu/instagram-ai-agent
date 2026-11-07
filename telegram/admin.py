from pathlib import Path
from typing import Any
from config import settings
from utils.helpers import get_vps_resource_snapshot


class AdminCommandController:
    """
    Executes Owner-only administrative operations and formats structured Persian
    status reports with explicit operation states:
    [در حال بررسی | در حال پردازش | نیازمند تأیید | موفق | ناموفق]
    """

    def __init__(self, manager: Any) -> None:
        self.m = manager

    async def render_status_card(self) -> str:
        ig_state = "Connected" if self.m.ig_client.is_connected else "Disconnected"
        ai_cfg = self.m.model_manager.get_masked_config()
        ai_state = "Connected" if settings.AI_API_KEY else "Missing Key"
        vps = get_vps_resource_snapshot()
        last_action = await self.m.lt_memory.get_preference(
            "last_activity_summary", "بررسی اولیه Session و آماده‌سازی ایجنت انجام شد."
        )

        return (
            "🤖 <b>Instagram AI Agent</b>\n"
            f"وضعیت: <b>{'متوقف (Paused)' if self.m.agent.is_paused else 'فعال'}</b>\n"
            f"اکانت: <code>{ig_state} ({self.m.ig_client.active_username or 'N/A'})</code>\n"
            f"هوش مصنوعی: <code>{ai_state} ({ai_cfg['model_name']})</code>\n"
            "تلگرام: <code>Connected</code>\n"
            f"مسیر Session: <code>{self.m.ig_client.session_manager.session_path}</code>\n"
            f"پروکسی: <code>{self.m.ig_client.proxy_manager.get_masked_proxy()}</code>\n"
            f"مصرف منابع هاست: <code>RAM {vps['ram_used_mb']}MB / Disk Free {vps['disk_free_mb']}MB</code>\n\n"
            f"📌 <b>آخرین فعالیت:</b>\n{last_action}"
        )

    async def render_stats_card(self) -> str:
        overview = await self.m.ig_analytics.get_account_overview()
        if overview.get("status") != "ok":
            return (
                "📊 <b>آمار اکانت اینستاگرام</b>\n"
                "وضعیت عملیات: <b>ناموفق</b>\n"
                f"علت: {overview.get('error', 'عدم اتصال Session')}"
            )

        acc = overview["account"]
        met = overview["metrics"]
        act = overview["agent_activity"]

        return (
            "📈 <b>گزارش جامع آمار اکانت (داده واقعی Instagrapi)</b>\n"
            "وضعیت عملیات: <b>موفق</b>\n\n"
            f"• نام کاربری: <code>@{acc['username']}</code> ({acc['full_name']})\n"
            f"• شناسه اکانت (PK): <code>{acc['pk']}</code>\n"
            f"• دنبال‌کنندگان: <code>{acc['followers']:,}</code>\n"
            f"• دنبال‌شوندگان: <code>{acc['following']:,}</code>\n"
            f"• کل پست‌ها: <code>{acc['media_count']}</code>\n"
            f"• میانگین نرخ تعامل: <code>{met['engagement_rate_percent']}%</code>\n"
            f"• بازدید پست‌های اخیر: <code>{met['total_views_sampled']:,}</code>\n"
            "• آمار Reach / Saves / Shares: <code>Unsupported (در اتصال خصوصی Instagrapi)</code>\n\n"
            "🤖 <b>عملکرد خودمختار ایجنت:</b>\n"
            f"• پست‌های منتشرشده توسط ایجنت: <code>{act['posts_published_by_agent']}</code>\n"
            f"• دایرکت‌های پاسخ‌داده‌شده: <code>{act['direct_messages_replied']}</code>\n"
            f"• کامنت‌های پاسخ‌داده‌شده: <code>{act['comments_replied']}</code>"
        )

    def read_recent_logs(self, lines_count: int = 20) -> str:
        log_path = Path(settings.LOG_FILE_PATH)
        if not log_path.exists():
            return "📜 هنوز فایلی در مسیر لاگ ثبت نشده است."
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        snippet = "\n".join(lines[-lines_count:])
        return f"📜 <b>آخرین لاگ‌های سیستم (بدون اطلاعات محرمانه):</b>\n<pre>{snippet[-3200:]}</pre>"
