from typing import Any, Dict
import psutil
from pathlib import Path
from config import settings


def get_current_datetime(timezone: str = "Asia/Tehran") -> Dict[str, Any]:
    """
    Current date and time in a given timezone, with the Persian (Jalali)
    calendar date and Persian weekday. No web access needed — the server's
    own clock answers time/date questions directly.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(timezone)
    except Exception:
        timezone = "Asia/Tehran"
        tz = ZoneInfo(timezone)
    now = datetime.now(tz)
    fa_weekdays = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]
    jalali_date = ""
    try:
        import jdatetime

        jd = jdatetime.datetime.fromgregorian(datetime=now.replace(tzinfo=None))
        fa_months = [
            "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
            "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
        ]
        jalali_date = f"{jd.day} {fa_months[jd.month - 1]} {jd.year}"
    except Exception:
        pass
    return {
        "timezone": timezone,
        "time": now.strftime("%H:%M"),
        "date_gregorian": now.strftime("%Y-%m-%d"),
        "date_jalali": jalali_date,
        "weekday_fa": fa_weekdays[now.weekday()],
        "weekday_en": now.strftime("%A"),
    }


def get_vps_resource_snapshot() -> Dict[str, Any]:
    """Measures current RAM, CPU, and Storage consumption on the Linux host."""
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage(str(Path(".").resolve()))
    return {
        "ram_used_mb": round(vm.used / (1024 * 1024), 1),
        "ram_total_mb": round(vm.total / (1024 * 1024), 1),
        "ram_percent": vm.percent,
        "cpu_percent": psutil.cpu_percent(interval=0.1),
        "disk_free_mb": round(disk.free / (1024 * 1024), 1),
        "log_file_path": settings.LOG_FILE_PATH,
    }
