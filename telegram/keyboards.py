from typing import Any, Dict, List
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def build_main_menu_keyboard(is_owner: bool) -> InlineKeyboardMarkup:
    if not is_owner:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🔍 جست‌وجوی اینترنتی به‌روز (2026)", callback_data="user_search_help"),
                    InlineKeyboardButton(text="💬 راهنمای پرسش و پاسخ", callback_data="cmd_help"),
                ]
            ]
        )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 وضعیت ایجنت (/status)", callback_data="cmd_status"),
                InlineKeyboardButton(text="📈 آمار اکانت (/stats)", callback_data="cmd_stats"),
            ],
            [
                InlineKeyboardButton(text="🤖 مدیریت چند مدل هوش مصنوعی (/مدل)", callback_data="cmd_models_menu"),
                InlineKeyboardButton(text="⚙️ مدیریت تسک‌های خودکار (/schedule)", callback_data="cmd_schedule"),
            ],
            [
                InlineKeyboardButton(text="🔐 ارسال Session اینستاگرام (JSON)", callback_data="wiz_ig_session"),
                InlineKeyboardButton(text="📱 اتصال حساب تلگرام (API ID/HASH)", callback_data="wiz_tg_login"),
            ],
            [
                InlineKeyboardButton(text="🖼 مدیریت پست‌ها (/posts)", callback_data="cmd_posts"),
                InlineKeyboardButton(text="📱 مدیریت استوری (/stories)", callback_data="cmd_stories"),
            ],
            [
                InlineKeyboardButton(text="✉️ مدیریت دایرکت (/messages)", callback_data="cmd_messages"),
                InlineKeyboardButton(text="💬 مدیریت کامنت‌ها (/comments)", callback_data="cmd_comments"),
            ],
            [
                InlineKeyboardButton(text="🚀 انتشار محتوا (/publish)", callback_data="cmd_publish"),
                InlineKeyboardButton(text="📢 مدیریت کانال‌های منبع (/channels)", callback_data="cmd_channels"),
            ],
            [
                InlineKeyboardButton(text="🔍 پست‌های تکراری (/duplicates)", callback_data="cmd_duplicates"),
                InlineKeyboardButton(text="📉 پست‌های کم‌بازدید (/lowviews)", callback_data="cmd_lowviews"),
            ],
            [
                InlineKeyboardButton(text="📦 نصب پکیج و توسعه (/packages)", callback_data="cmd_packages"),
                InlineKeyboardButton(text="🧠 حافظه دائمی (/memory)", callback_data="cmd_memory"),
            ],
            [
                InlineKeyboardButton(text="⚙️ تنظیمات (/settings)", callback_data="cmd_settings"),
                InlineKeyboardButton(text="📜 لاگ فعالیت‌ها (/logs)", callback_data="cmd_logs"),
            ],
            [
                InlineKeyboardButton(text="⏸ توقف خودکار (/pause)", callback_data="cmd_pause"),
                InlineKeyboardButton(text="▶️ ادامه فعالیت (/resume)", callback_data="cmd_resume"),
            ],
        ]
    )


def build_models_menu_keyboard(models: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="➕ افزودن API و مدل جدید (تست زنده)", callback_data="wiz_add_model")]
    ]
    for m in models:
        badge = "🟢 فعال:" if m["is_active"] else "⚪️ انتخاب:"
        rows.append([
            InlineKeyboardButton(
                text=f"{badge} {m['model_name']}",
                callback_data=f"mdl_select:{m['model_id']}",
            ),
            InlineKeyboardButton(
                text="🔄 تست مجدد",
                callback_data=f"mdl_retest:{m['model_id']}",
            ),
            InlineKeyboardButton(
                text="🗑 حذف",
                callback_data=f"mdl_delete:{m['model_id']}",
            ),
        ])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت به منوی اصلی", callback_data="cmd_start")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_tasks_menu_keyboard(tasks: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for t in tasks[:8]:
        tid = t["task_id"]
        if not t["approved_by_owner"]:
            rows.append([
                InlineKeyboardButton(
                    text=f"✅ تأیید و شروع تسک ({t['title'][:22]})",
                    callback_data=f"tsk_approve:{tid}",
                ),
                InlineKeyboardButton(text="🗑 حذف", callback_data=f"tsk_delete:{tid}"),
            ])
        else:
            toggle_btn = (
                InlineKeyboardButton(text=f"⏸ توقف ({tid})", callback_data=f"tsk_pause:{tid}")
                if t["enabled"]
                else InlineKeyboardButton(text=f"▶️ ادامه ({tid})", callback_data=f"tsk_resume:{tid}")
            )
            rows.append([
                toggle_btn,
                InlineKeyboardButton(text="🗑 حذف", callback_data=f"tsk_delete:{tid}"),
            ])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت به منوی اصلی", callback_data="cmd_start")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_confirmation_keyboard(action_prefix: str, target_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ تأیید نهایی و اجرا",
                    callback_data=f"confirm_{action_prefix}:{target_id}",
                ),
                InlineKeyboardButton(
                    text="❌ لغو عملیات",
                    callback_data="cancel_action",
                ),
            ]
        ]
    )


def build_back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 بازگشت به منوی اصلی", callback_data="cmd_start")]
        ]
    )
