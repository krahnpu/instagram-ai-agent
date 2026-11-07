import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from config import settings


class SecretMaskingFilter(logging.Filter):
    """Prevents tokens, API keys, passwords, and session strings from appearing in logs."""

    PATTERNS = [
        re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
        re.compile(r"(sessionid[\"']?\s*:\s*[\"'])[^\"']+([\"'])", re.IGNORECASE),
        re.compile(r"(Authorization[\"']?\s*:\s*[\"'])[^\"']+([\"'])", re.IGNORECASE),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if settings.TELEGRAM_BOT_TOKEN and len(settings.TELEGRAM_BOT_TOKEN) > 8:
            msg = msg.replace(settings.TELEGRAM_BOT_TOKEN, "[REDACTED_TG_TOKEN]")
        if settings.AI_API_KEY and len(settings.AI_API_KEY) > 8:
            msg = msg.replace(settings.AI_API_KEY, "[REDACTED_AI_KEY]")
        for pattern in self.PATTERNS:
            msg = pattern.sub(r"\1[REDACTED]\2" if pattern.groups > 1 else r"\1[REDACTED]", msg)
        record.msg = msg
        record.args = ()
        return True


def setup_logger() -> None:
    log_path = Path(settings.LOG_FILE_PATH)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("ig_agent")
    root.setLevel(logging.INFO)
    if root.handlers:
        return

    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    mask_filter = SecretMaskingFilter()

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=settings.LOG_MAX_BYTES,
        backupCount=2,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.addFilter(mask_filter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    console_handler.addFilter(mask_filter)

    root.addHandler(file_handler)
    root.addHandler(console_handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"ig_agent.{name}")
