import os
from pathlib import Path
from typing import Tuple
from dotenv import load_dotenv

load_dotenv()


def load_token_txt(file_path: Path = Path("token.txt")) -> Tuple[int, str]:
    """
    Reads the 2-line bootstrap configuration file `token.txt`:
    Line 1: Owner numerical Telegram ID (OWNER_ID)
    Line 2: Telegram Bot Token (BOT_TOKEN)
    Falls back to .env variables if token.txt is missing or contains placeholder text.
    """
    env_owner = int(os.getenv("TELEGRAM_OWNER_ID", "0") or "0")
    env_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

    if not file_path.exists():
        alt_path = Path(__file__).resolve().parent / "token.txt"
        if alt_path.exists():
            file_path = alt_path
        else:
            return env_owner, env_token

    try:
        lines = [
            line.strip()
            for line in file_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        owner_id = env_owner
        bot_token = env_token
        if len(lines) >= 1 and lines[0].isdigit():
            owner_id = int(lines[0])
        if len(lines) >= 2 and ":" in lines[1] and "YOUR_TELEGRAM_BOT_TOKEN" not in lines[1]:
            bot_token = lines[1]
        return owner_id, bot_token
    except Exception:
        return env_owner, env_token


_BOOT_OWNER_ID, _BOOT_BOT_TOKEN = load_token_txt()


class Settings:
    """
    Centralized configuration with zero-code `token.txt` bootstrap support.
    All other settings (AI models, Instagram sessions, Telethon credentials,
    proxies, and tasks) are dynamically configured and persisted via Telegram.
    """

    # 1. Bootstrap Telegram Credentials (loaded from token.txt first, then .env)
    TELEGRAM_OWNER_ID: int = _BOOT_OWNER_ID
    TELEGRAM_BOT_TOKEN: str = _BOOT_BOT_TOKEN

    # 2. Optional Telethon User Account Configuration (Can be set interactively in Telegram)
    TG_API_ID: int = int(os.getenv("TG_API_ID", "0") or "0")
    TG_API_HASH: str = os.getenv("TG_API_HASH", "")
    TG_SESSION_PATH: str = os.getenv("TG_SESSION_PATH", "data/telethon_user.session")

    # 3. OpenAI-Compatible AI API Configuration (Managed interactively via /start & /مدل)
    AI_API_BASE_URL: str = os.getenv("AI_API_BASE_URL", "")
    AI_API_KEY: str = os.getenv("AI_API_KEY", "")
    AI_MODEL_NAME: str = os.getenv("AI_MODEL_NAME", "")
    AI_TEMPERATURE: float = float(os.getenv("AI_TEMPERATURE", "0.4"))
    AI_MAX_TOKENS: int = int(os.getenv("AI_MAX_TOKENS", "1500"))
    AI_TIMEOUT: int = int(os.getenv("AI_TIMEOUT", "45"))

    # 4. Instagram Session Configuration
    IG_SESSION_PATH: str = os.getenv("IG_SESSION_PATH", "/tmp/ig_api/session.json")
    IG_ACCOUNTS_DIR: str = os.getenv("IG_ACCOUNTS_DIR", "data/ig_sessions")
    SESSION_ENCRYPTION_KEY: str = os.getenv("SESSION_ENCRYPTION_KEY", "ig-agent-default-vault-key-2026")

    # 5. Instagram Proxy Configuration
    PROXY_ENABLED: bool = os.getenv("PROXY_ENABLED", "false").lower() in ("true", "1", "yes")
    PROXY_URL: str = os.getenv("PROXY_URL", "")
    PROXY_USERNAME: str = os.getenv("PROXY_USERNAME", "")
    PROXY_PASSWORD: str = os.getenv("PROXY_PASSWORD", "")

    # 6. Independent Up-to-Date Web Search Configuration
    SEARCH_PROVIDER: str = os.getenv("SEARCH_PROVIDER", "duckduckgo")
    SEARCH_API_KEY: str = os.getenv("SEARCH_API_KEY", "")
    SEARCH_MAX_RESULTS: int = int(os.getenv("SEARCH_MAX_RESULTS", "6"))

    # 7. Database & Storage Configuration (Optimized for 2GB RAM / 2GB Storage Linux VPS)
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///data/agent_memory.db")
    TEMP_MEDIA_DIR: str = os.getenv("TEMP_MEDIA_DIR", "data/temp_media")
    BACKUP_DIR: str = os.getenv("BACKUP_DIR", "data/backups")
    MAX_TEMP_STORAGE_MB: int = int(os.getenv("MAX_TEMP_STORAGE_MB", "350"))
    LOG_FILE_PATH: str = os.getenv("LOG_FILE_PATH", "logs/agent.log")
    LOG_MAX_BYTES: int = int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024)))

    # 8. Operational Safety & Rate Limits
    ACTION_MIN_DELAY_SEC: int = int(os.getenv("ACTION_MIN_DELAY_SEC", "4"))
    ACTION_MAX_DELAY_SEC: int = int(os.getenv("ACTION_MAX_DELAY_SEC", "12"))
    MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "2"))
    AUTO_PUBLISH_REQUIRE_APPROVAL: bool = os.getenv("AUTO_PUBLISH_REQUIRE_APPROVAL", "true").lower() == "true"


settings = Settings()

Path("data").mkdir(parents=True, exist_ok=True)
Path("logs").mkdir(parents=True, exist_ok=True)
Path(settings.TEMP_MEDIA_DIR).mkdir(parents=True, exist_ok=True)
Path(settings.BACKUP_DIR).mkdir(parents=True, exist_ok=True)
Path(settings.IG_ACCOUNTS_DIR).mkdir(parents=True, exist_ok=True)
