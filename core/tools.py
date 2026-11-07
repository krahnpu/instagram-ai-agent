import importlib
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List
from config import settings
from core.permissions import Role, PermissionManager
from utils.helpers import get_vps_resource_snapshot, get_current_datetime
from utils.logger import get_logger


logger = get_logger("core.tools")


def _validate_tool_schema(schema: Dict[str, Any]) -> str:
    """Check an extension tool schema is a valid OpenAI-style function
    schema AND JSON-serializable. Returns an error message, or "" if valid.

    A schema with the wrong shape (e.g. type "object" instead of
    "function") serializes fine as JSON but the AI provider rejects the
    whole request with HTTP 400 "Input should be 'function'" — and since
    the bad schema stays registered, EVERY later AI call fails the same
    way. So shape is validated here, not just serializability.
    """
    if not isinstance(schema, dict):
        return "اسکیما باید یک دیکشنری باشد."
    if schema.get("type") != "function":
        return f"نوع ابزار (type) باید 'function' باشد، نه {schema.get('type')!r}."
    fn = schema.get("function")
    if not isinstance(fn, dict):
        return "بخش function اسکیما باید یک دیکشنری باشد."
    name = fn.get("name")
    if not isinstance(name, str) or not name.strip():
        return "نام ابزار (function.name) باید یک رشته غیرخالی باشد."
    params = fn.get("parameters", {"type": "object", "properties": {}})
    if not isinstance(params, dict):
        return "بخش parameters باید یک دیکشنری باشد."
    if params.get("type", "object") != "object":
        return "نوع parameters باید 'object' باشد."
    if not isinstance(params.get("properties", {}), dict):
        return "بخش properties باید یک دیکشنری باشد."
    try:
        json.dumps(schema, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        return f"اسکیما قابل تبدیل به JSON نیست: {exc}"
    return ""


def load_extensions(registry: "ToolRegistry") -> List[str]:
    """Import every module in extensions/ and let it register its own tools
    via register(registry). A broken module is logged and skipped — it can
    never crash the bot. Called at startup (main.py) and after
    save_code_extension so new tools go live immediately."""
    ext_dir = Path("extensions")
    loaded: List[str] = []
    if not ext_dir.exists():
        return loaded
    if str(ext_dir.resolve()) not in sys.path:
        sys.path.insert(0, str(ext_dir.resolve()))
    for mod_path in sorted(ext_dir.glob("*.py")):
        name = mod_path.stem
        try:
            module = importlib.import_module(name)
            importlib.reload(module)
            register = getattr(module, "register", None)
            if callable(register):
                register(registry)
                loaded.append(name)
            else:
                logger.warning(f"Extension {name} has no register(registry); skipped.")
        except Exception as exc:
            logger.error(f"Extension {name} failed to load: {exc}")
    return loaded


class ToolRegistry:
    """
    Registers all agent tools with OpenAI Function Calling JSON schemas
    and dispatches tool invocations to the underlying modular subsystems.
    """

    def __init__(
        self,
        ig_client: Any,
        ig_publisher: Any,
        ig_stories: Any,
        ig_direct: Any,
        ig_comments: Any,
        ig_interactions: Any,
        ig_analytics: Any,
        ig_music: Any,
        ig_content_manager: Any,
        search_engine: Any,
        lt_memory: Any,
        user_connector: Any,
        scheduler: Any = None,
        self_extender: Any = None,
    ) -> None:
        self.ig_client = ig_client
        self.ig_publisher = ig_publisher
        self.ig_stories = ig_stories
        self.ig_direct = ig_direct
        self.ig_comments = ig_comments
        self.ig_interactions = ig_interactions
        self.ig_analytics = ig_analytics
        self.ig_music = ig_music
        self.ig_content_manager = ig_content_manager
        self.search_engine = search_engine
        self.lt_memory = lt_memory
        self.user_connector = user_connector
        self.scheduler = scheduler
        self.self_extender = self_extender
        self.permissions = PermissionManager()
        # Tools added at runtime by self-written extensions (see
        # load_extensions). Schemas are exposed to the owner only.
        self.extension_tools: Dict[str, Any] = {}
        self.extension_schemas: List[Dict[str, Any]] = []
        # Aiogram Bot used to send files to the owner; wired in main.py after
        # TelegramBotManager is created (tool registry is built first).
        # NOTE: TelegramBotManager.bot is None until start() runs, so store the
        # manager itself and resolve .bot lazily at send time — wiring the
        # .bot attribute early silently stored None and every file send failed.
        self.file_sender_source: Any = None

    def register_extension_tool(self, name: str, schema: Dict[str, Any], handler: Callable[..., Any]) -> None:
        """Register one tool from a self-written extension module. The
        handler may be sync or async and receives the tool arguments dict.

        The schema must be a valid OpenAI function-calling schema made of
        plain JSON data: {"type": "function", "function": {"name": ...,
        "description": ..., "parameters": {...}}} — never the handler
        function or any other Python object inside it, and never type
        "object" at the top level. A bad schema would break EVERY later
        AI call, so it is rejected here instead of registered.
        """
        err = _validate_tool_schema(schema)
        if err:
            logger.error(f"Extension tool '{name}' schema invalid, rejected: {err}")
            raise ValueError(
                f"اسکیمای ابزار '{name}' درست نیست ({err}). "
                "قالب درست فقط این است: {\"type\": \"function\", \"function\": {\"name\": \"<نام ابزار>\", \"description\": \"<چه کار می‌کند>\", \"parameters\": {\"type\": \"object\", \"properties\": {...}, \"required\": [...]}}}. "
                "تابع را فقط به عنوان آرگومان سوم register_extension_tool بده و هرگز داخل اسکیما نگذار."
            )
        self.extension_tools[name] = handler
        self.extension_schemas = [s for s in self.extension_schemas if s["function"]["name"] != name]
        self.extension_schemas.append(schema)

    def set_file_sender(self, source: Any) -> None:
        self.file_sender_source = source

    def _resolve_file_sender_bot(self) -> Any:
        source = self.file_sender_source
        if source is None:
            return None
        return getattr(source, "bot", source)

    async def _send_file_to_owner(
        self,
        filename: str = "",
        content: str = "",
        file_path: str = "",
        caption: str = "",
    ) -> Dict[str, Any]:
        """Send a file to the owner as a Telegram document. Either builds the
        file from text content (filename + content) or sends an existing
        server file (file_path). The filename is sanitised to a bare name so
        no path traversal is possible; content is capped at 5 MB."""
        file_sender_bot = self._resolve_file_sender_bot()
        if file_sender_bot is None:
            return {"status": "error", "error": "ارسال فایل در دسترس نیست."}
        try:
            if file_path:
                path = Path(file_path)
                if not path.exists() or not path.is_file():
                    return {"status": "error", "error": f"فایل یافت نشد: {file_path}"}
                # Never send a 0-byte file to Telegram: it rejects it with
                # "file must be non-empty" and the raw error ends up in chat.
                if path.stat().st_size == 0:
                    return {"status": "error", "error": "فایل خالی است؛ اول محتوا را کامل بنویس بعد بفرست."}
            else:
                if "/" in filename or "\\" in filename or not filename.strip() or filename.strip() in {".", ".."}:
                    return {"status": "error", "error": "نام فایل معتبر نیست. فقط نام فایل بدهید، بدون پوشه."}
                # The model sometimes calls this tool with empty content on
                # its first attempt — reject it here with an actionable
                # instruction instead of letting Telegram fail cryptically.
                if not content.strip():
                    return {"status": "error", "error": "محتوای فایل خالی است؛ کد کامل را در همان فراخوانی بنویس و دوباره بفرست. هرگز با محتوای خالی ابزار را صدا نزن."}
                safe_name = Path(filename).name
                if len(content.encode("utf-8")) > 5 * 1024 * 1024:
                    return {"status": "error", "error": "محتوای فایل بزرگ‌تر از ۵ مگابایت است."}
                out_dir = Path(settings.TEMP_MEDIA_DIR)
                out_dir.mkdir(parents=True, exist_ok=True)
                path = out_dir / safe_name
                path.write_text(content, encoding="utf-8")
            from aiogram.types import FSInputFile

            await file_sender_bot.send_document(
                settings.TELEGRAM_OWNER_ID,
                FSInputFile(str(path)),
                caption=caption or None,
            )
            return {"status": "ok", "filename": path.name, "message": "فایل برای شما ارسال شد."}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    def get_openai_tool_schemas(self, role: Role) -> List[Dict[str, Any]]:
        all_tools: List[Dict[str, Any]] = [
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Search the live internet for up-to-date facts, versions, prices, or news and extract verified sources.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Search query in Persian or English"}
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_instagram_profile",
                    "description": "Fetch live Instagram account profile, followers count, following count, and media count via Instagrapi.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_instagram_posts",
                    "description": "Fetch recent posts/Reels from the connected Instagram account with real metrics.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "amount": {"type": "integer", "description": "Number of recent posts to fetch (default 12)"}
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "scan_duplicate_posts",
                    "description": "Scan account posts for exact or suspicious duplicates using Perceptual Hash (pHash), video frames, and caption similarity.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "amount": {"type": "integer", "description": "Number of posts to analyze (default 20)"}
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "find_low_view_posts",
                    "description": "Find Instagram posts with views/likes below threshold and older than min_age_days.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "min_views": {"type": "integer", "description": "Minimum view count threshold (e.g. 1000)"},
                            "min_age_days": {"type": "integer", "description": "Minimum age of post in days (e.g. 30)"},
                        },
                        "required": ["min_views"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "delete_instagram_posts",
                    "description": "Delete specified Instagram media PKs after explicit Owner confirmation.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "media_pks": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "List of Instagram media PK IDs to delete",
                            },
                            "confirmed_by_owner": {
                                "type": "boolean",
                                "description": "Must be True only if Owner explicitly confirmed deletion",
                            },
                        },
                        "required": ["media_pks", "confirmed_by_owner"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "publish_instagram_photo",
                    "description": "Publish a photo post to Instagram with smart caption and hashtags.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "photo_path": {"type": "string", "description": "Local file path to image"},
                            "caption": {"type": "string", "description": "Full post caption including hashtags"},
                        },
                        "required": ["photo_path", "caption"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "publish_instagram_reel",
                    "description": "Publish a video Reel to Instagram via Instagrapi clip_upload. ALWAYS pass the thumbnail when you have one (e.g. thumbnail_path returned by fetch_telegram_channel_media) — without it the upload needs MoviePy/ffmpeg on the server. The publisher also extracts a keyframe itself when no thumbnail is given.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "video_path": {"type": "string", "description": "Local file path to MP4 video"},
                            "caption": {"type": "string", "description": "Reel caption and hashtags"},
                            "thumbnail": {"type": "string", "description": "Optional local path to a JPG cover image (e.g. the Telegram video thumb). Pass it whenever fetch_telegram_channel_media returned a thumbnail_path."},
                        },
                        "required": ["video_path", "caption"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "publish_instagram_story",
                    "description": "Publish a photo or video Story to Instagram.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "media_path": {"type": "string", "description": "Local path to story image or video"},
                            "caption": {"type": "string", "description": "Optional story caption"},
                        },
                        "required": ["media_path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_recent_direct_messages",
                    "description": "Fetch unread or recent Instagram Direct Message threads via Instagrapi.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "amount": {"type": "integer", "description": "Number of threads to fetch (default 10)"}
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "reply_direct_message",
                    "description": "Send a response message to an Instagram Direct thread.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "thread_id": {"type": "string", "description": "Instagram DM thread ID"},
                            "text": {"type": "string", "description": "Response text to send"},
                        },
                        "required": ["thread_id", "text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_post_comments",
                    "description": "Fetch recent comments on a specific Instagram post.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "media_pk": {"type": "string", "description": "Instagram media PK"},
                        },
                        "required": ["media_pk"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "reply_post_comment",
                    "description": "Reply to a specific comment on an Instagram post without duplicate replies.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "media_pk": {"type": "string", "description": "Instagram media PK"},
                            "comment_pk": {"type": "string", "description": "Comment PK to reply to"},
                            "text": {"type": "string", "description": "Reply text"},
                        },
                        "required": ["media_pk", "comment_pk", "text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "fetch_telegram_channel_media",
                    "description": "Fetch latest video or photo from a configured Telegram channel via Telethon. For videos it also downloads Telegram's own thumbnail and returns it as thumbnail_path — pass that to publish_instagram_reel so no MoviePy/ffmpeg is needed.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "channel_username": {"type": "string", "description": "Channel username e.g. KRALMOVIE"},
                            "limit": {"type": "integer", "description": "Number of messages to inspect"},
                        },
                        "required": ["channel_username"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "save_admin_preference",
                    "description": "Store Owner preference, tone style, or rule in persistent long-term memory.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string", "description": "Preference key e.g. page_tone_style"},
                            "value": {"type": "string", "description": "Preference value"},
                        },
                        "required": ["key", "value"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "create_scheduled_task",
                    "description": "Create an autonomous recurring, cron, or event-driven task from natural language instruction.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "instruction": {
                                "type": "string",
                                "description": "Natural language task description e.g. هر ۳۰ ثانیه دایرکت‌ها را بررسی کن",
                            }
                        },
                        "required": ["instruction"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "start_telegram_login",
                    "description": "Start Telethon user-account login: send a Telegram login code to the owner's phone using saved TG_API_ID and TG_API_HASH when available. If they are missing, ask the owner right here in chat for the API ID, API hash and phone number, then call this tool again with all three — the credentials are saved automatically (encrypted) and no code or variable change is ever needed. Use this when the owner asks to log in to Telegram or sends a phone number.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "phone": {"type": "string", "description": "Phone number with country code, e.g. +17628218662"},
                            "api_id": {"type": "integer", "description": "Telegram API ID (optional, saved credentials are used when omitted)"},
                            "api_hash": {"type": "string", "description": "Telegram API hash (optional, saved credentials are used when omitted)"},
                        },
                        "required": ["phone"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "submit_telegram_code",
                    "description": "Submit the Telegram login verification code received by SMS/Telegram.",
                    "parameters": {
                        "type": "object",
                        "properties": {"code": {"type": "string", "description": "Verification code, digits only"}},
                        "required": ["code"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "submit_telegram_2fa",
                    "description": "Submit the Telegram two-step verification (2FA) cloud password.",
                    "parameters": {
                        "type": "object",
                        "properties": {"password": {"type": "string", "description": "Two-step verification password"}},
                        "required": ["password"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "import_instagram_session",
                    "description": "Import an Instagram session from pasted JSON (Instagrapi settings, browser cookie array, or flat cookie dict), save it and verify the connection. Use this when the owner pastes session JSON or cookies.",
                    "parameters": {
                        "type": "object",
                        "properties": {"raw_json": {"type": "string", "description": "Raw session/cookie JSON text"}},
                        "required": ["raw_json"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "test_instagram_connection",
                    "description": "Test the current Instagram session connection and report the connected username or login_required.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "create_one_off_reminder",
                    "description": "Send the owner a message in this chat after a one-off delay (e.g. in 30 seconds, in 10 minutes). This is a single reminder, not a recurring task, and needs no approval.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "delay_seconds": {"type": "integer", "description": "Delay in seconds, e.g. 30"},
                            "message": {"type": "string", "description": "Message text to send when the time is up"},
                        },
                        "required": ["delay_seconds", "message"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "delete_scheduled_task",
                    "description": "Permanently delete a scheduled task by its ID (e.g. tsk_a6ed388f). Use this when the owner asks to delete/remove a task. Never create a new task to delete one.",
                    "parameters": {
                        "type": "object",
                        "properties": {"task_id": {"type": "string", "description": "Task ID, e.g. tsk_a6ed388f"}},
                        "required": ["task_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "pause_scheduled_task",
                    "description": "Pause (stop) a scheduled task by its ID without deleting it.",
                    "parameters": {
                        "type": "object",
                        "properties": {"task_id": {"type": "string", "description": "Task ID, e.g. tsk_a6ed388f"}},
                        "required": ["task_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "resume_scheduled_task",
                    "description": "Resume a paused scheduled task by its ID.",
                    "parameters": {
                        "type": "object",
                        "properties": {"task_id": {"type": "string", "description": "Task ID, e.g. tsk_a6ed388f"}},
                        "required": ["task_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_scheduled_tasks",
                    "description": "List all scheduled tasks with their IDs, titles, intervals and states.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "send_message_via_user_account",
                    "description": "Send a message FROM the connected Telegram user account (userbot, the owner's own account) TO the owner as a real DM. Use the user's EXACT words verbatim as the message — never substitute your own generic test text. ALWAYS pass the owner's @username (e.g. Dev_kral) when you know it — without it the message may only reach Saved Messages. If the user account is not connected, report that instead of sending via the bot. The result tells you exactly where the message went (sent_via); report that destination honestly.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "message": {"type": "string", "description": "Message text to send from the user account"},
                            "username": {"type": "string", "description": "Owner's Telegram @username without @, e.g. Dev_kral. Pass it whenever known."},
                        },
                        "required": ["message"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_telegram_user_account_status",
                    "description": "Report which Telegram account the userbot session is actually logged into (username, user id, phone) and whether it is connected. Use this whenever the owner asks which account is connected, or before claiming anything about the user account connection — never guess the account from chat history.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_current_datetime",
                    "description": "Get the current date and time right now (server clock) in a timezone, with the Persian (Jalali) date and Persian weekday. Use this for ANY question about the current time or date (what time is it, what is today's date, in Iran or any city) — never use web_search for that and never claim you cannot know the time.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "timezone": {"type": "string", "description": "IANA timezone, e.g. Asia/Tehran for Iran, America/Montreal for Montreal. Default Asia/Tehran."},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "create_and_send_file",
                    "description": "Create a file from text content and send it to the owner as a real Telegram document attachment. Use this whenever the owner asks for a file (for example an HTML game, a script, a text file): build the complete content and send it as a file — never paste long code as a chat message instead. Filename only, no folders.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "filename": {"type": "string", "description": "File name only, e.g. snake.html"},
                            "content": {"type": "string", "description": "Complete file content"},
                            "caption": {"type": "string", "description": "Short caption for the file message"},
                        },
                        "required": ["filename", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "send_file_to_owner",
                    "description": "Send an existing file from the server to the owner as a Telegram document attachment.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string", "description": "Local path of the file on the server"},
                            "caption": {"type": "string", "description": "Short caption for the file message"},
                        },
                        "required": ["file_path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_server_resources",
                    "description": "Report the live server resources this agent runs on: CPU percent, RAM used/total, free disk. Use this whenever the owner asks how much server resources, CPU, RAM or disk are available or used — never guess and never claim you cannot check it.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "propose_package_installation",
                    "description": "Propose installing a required Python package in the virtualenv, awaiting Owner confirmation.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "package_name": {"type": "string", "description": "PyPI package name"},
                            "version_spec": {"type": "string", "description": "Recommended version or latest"},
                            "reason": {"type": "string", "description": "Technical reason why the package is needed"},
                        },
                        "required": ["package_name", "reason"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "approve_and_install_package",
                    "description": "Install a previously proposed package AFTER the owner explicitly confirmed it in chat (they said install it / تایید). Pass the request_id from propose_package_installation and confirmed_by_owner=true. Runs pip install and verifies the import. Never call this without the owner's explicit confirmation for that exact package.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "request_id": {"type": "string", "description": "Request id returned by propose_package_installation"},
                            "confirmed_by_owner": {"type": "boolean", "description": "Must be true — only set it after the owner confirmed this exact install in chat."},
                        },
                        "required": ["request_id", "confirmed_by_owner"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_pending_package_requests",
                    "description": "List package-install proposals waiting for the owner's confirmation.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "save_code_extension",
                    "description": "Save a self-written extension module (Python code you wrote to add a missing capability) under extensions/<name>.py, after the owner approved adding it. The code is syntax-checked; a broken module is rejected. After saving, it is loaded live and its tools become available. Tell the owner the extension lives only until the next redeploy unless it is added to the repo. Every tool schema MUST use this exact shape: {\"type\": \"function\", \"function\": {\"name\": \"<tool_name>\", \"description\": \"<what it does>\", \"parameters\": {\"type\": \"object\", \"properties\": {\"arg\": {\"type\": \"string\"}}, \"required\": [\"arg\"]}}}. Register with registry.register_extension_tool(\"<tool_name>\", <that schema dict>, <handler function>) — the handler goes ONLY as the third argument, never inside the schema, and the top-level type is always \"function\", never \"object\". A schema that breaks these rules is rejected and the tool never loads.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Short snake_case module name, e.g. telegram_dialogs"},
                            "code": {"type": "string", "description": "Full Python source of the module. It must define register(registry) and call registry.register_extension_tool(name, schema, handler) for each new tool, where schema is exactly {\"type\": \"function\", \"function\": {\"name\": name, \"description\": ..., \"parameters\": {\"type\": \"object\", \"properties\": {...}, \"required\": [...]}}} and handler is passed ONLY as the third argument, never inside the schema."},
                            "description": {"type": "string", "description": "What the extension adds and why"},
                        },
                        "required": ["name", "code"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_code_extensions",
                    "description": "List self-written extension modules currently saved.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_telegram_dialogs",
                    "description": "List the userbot's Telegram chats (users, groups, channels) with unread counts, so you can find a conversation and read its messages. Use this when the owner asks to read someone's Telegram messages via the user account.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "limit": {"type": "integer", "description": "How many chats to list (default 20, max 50)."},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_telegram_messages",
                    "description": "Read recent messages from one Telegram chat via the connected user account (userbot). Give a @username, numeric id or phone number as peer. Use this when the owner asks what someone said on Telegram.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "peer": {"type": "string", "description": "@username, numeric id or phone number of the chat"},
                            "limit": {"type": "integer", "description": "How many recent messages to read (default 10, max 50)."},
                        },
                        "required": ["peer"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "run_self_check",
                    "description": "Check the agent's own health right now: Instagram session file present, Telegram user account connected, disk and memory usage. Returns a short report and a list of problems found.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_recent_logs",
                    "description": "Read the last lines of the agent's own log file to diagnose a problem (failed tool, failed login, crash). Use this when something is not working and you need to find out why before telling the owner.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "lines": {"type": "integer", "description": "How many recent lines to read (default 50, max 200)."},
                        },
                    },
                },
            },
        ]

        if role == Role.OWNER:
            # Defense in depth: never let a bad extension schema poison the
            # AI request — one malformed schema breaks every
            # chat_completion call, so skip (and log) any that fail validation.
            safe_extension_schemas: List[Dict[str, Any]] = []
            for schema in self.extension_schemas:
                err = _validate_tool_schema(schema)
                if err:
                    bad_name = (schema or {}).get("function", {}).get("name", "?") if isinstance(schema, dict) else "?"
                    logger.error(f"Skipping invalid extension schema '{bad_name}': {err}")
                    continue
                safe_extension_schemas.append(schema)
            return all_tools + safe_extension_schemas

        return [
            t for t in all_tools
            if t["function"]["name"] in self.permissions.PUBLIC_TOOLS
        ]

    async def invoke(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if name == "web_search":
            return await self.search_engine.search_and_extract(args.get("query", ""))
        if name == "get_instagram_profile":
            return await self.ig_analytics.get_account_overview()
        if name == "list_instagram_posts":
            return await self.ig_content_manager.list_recent_media(amount=int(args.get("amount", 12)))
        if name == "scan_duplicate_posts":
            return await self.ig_content_manager.find_duplicate_posts(amount=int(args.get("amount", 20)))
        if name == "find_low_view_posts":
            return await self.ig_content_manager.find_low_performing_posts(
                min_views=int(args.get("min_views", 1000)),
                min_age_days=int(args.get("min_age_days", 0)),
            )
        if name == "delete_instagram_posts":
            return await self.ig_content_manager.delete_posts(
                media_pks=args.get("media_pks", []),
                confirmed=bool(args.get("confirmed_by_owner", False)),
            )
        if name == "publish_instagram_photo":
            return await self.ig_publisher.publish_photo(args["photo_path"], args.get("caption", ""))
        if name == "publish_instagram_reel":
            return await self.ig_publisher.publish_reel(
                args["video_path"], args.get("caption", ""), thumbnail=args.get("thumbnail")
            )
        if name == "publish_instagram_story":
            return await self.ig_stories.publish_story(args["media_path"], args.get("caption", ""))
        if name == "get_recent_direct_messages":
            return await self.ig_direct.fetch_inbox(amount=int(args.get("amount", 10)))
        if name == "reply_direct_message":
            return await self.ig_direct.send_reply(args["thread_id"], args["text"])
        if name == "get_post_comments":
            return await self.ig_comments.fetch_media_comments(args["media_pk"])
        if name == "reply_post_comment":
            return await self.ig_comments.reply_to_comment(
                args["media_pk"], args["comment_pk"], args["text"]
            )
        if name == "fetch_telegram_channel_media":
            return await self.user_connector.fetch_latest_channel_media(
                args["channel_username"], limit=int(args.get("limit", 5))
            )
        if name == "save_admin_preference":
            await self.lt_memory.set_preference(args["key"], args["value"])
            return {"status": "ok", "key": args["key"], "value": args["value"]}
        if name == "delete_scheduled_task" and self.scheduler:
            return await self.scheduler.delete_task(str(args.get("task_id", "")))
        if name == "pause_scheduled_task" and self.scheduler:
            return await self.scheduler.pause_task(str(args.get("task_id", "")))
        if name == "resume_scheduled_task" and self.scheduler:
            return await self.scheduler.resume_task(str(args.get("task_id", "")))
        if name == "list_scheduled_tasks" and self.scheduler:
            return {"status": "ok", "tasks": await self.scheduler.list_dynamic_tasks()}
        if name == "send_message_via_user_account" and self.user_connector:
            return await self.user_connector.send_message_to_owner(
                str(args.get("message", "")), username=args.get("username")
            )
        if name == "get_telegram_user_account_status" and self.user_connector:
            return await self.user_connector.get_account_status()
        if name == "get_server_resources":
            return {"status": "ok", **get_vps_resource_snapshot()}
        if name == "get_current_datetime":
            return {"status": "ok", **get_current_datetime(str(args.get("timezone") or "Asia/Tehran"))}
        if name == "create_and_send_file":
            return await self._send_file_to_owner(
                filename=str(args.get("filename", "")),
                content=str(args.get("content", "")),
                caption=str(args.get("caption", "")),
            )
        if name == "send_file_to_owner":
            return await self._send_file_to_owner(
                file_path=str(args.get("file_path", "")),
                caption=str(args.get("caption", "")),
            )
        if name == "create_scheduled_task" and self.scheduler:
            # Owner-only tool: owner's own tasks are auto-approved and mounted
            # immediately instead of waiting for an inline approval tap.
            return await self.scheduler.create_task_from_natural_language(
                args.get("instruction", ""), auto_approve=True
            )
        if name == "start_telegram_login" and self.user_connector:
            creds = await self.user_connector.get_saved_credentials()
            api_id = args.get("api_id") or creds.get("api_id")
            api_hash = args.get("api_hash") or creds.get("api_hash")
            if not api_id or not api_hash:
                return {"status": "error", "error": "API ID و API HASH تنظیم نشده‌اند. ابتدا آن‌ها را ارسال کنید."}
            return await self.user_connector.begin_interactive_login(
                api_id=int(api_id), api_hash=str(api_hash), phone=str(args.get("phone", "")).strip()
            )
        if name == "submit_telegram_code" and self.user_connector:
            return await self.user_connector.submit_verification_code(str(args.get("code", "")).strip())
        if name == "submit_telegram_2fa" and self.user_connector:
            return await self.user_connector.submit_two_factor_password(str(args.get("password", "")))
        if name == "import_instagram_session":
            return await self.ig_client.import_and_verify_session_json(str(args.get("raw_json", "")))
        if name == "test_instagram_connection":
            return await self.ig_client.initialize_session()
        if name == "create_one_off_reminder" and self.scheduler:
            return await self.scheduler.create_one_off_reminder(
                chat_id=0,
                delay_seconds=int(args.get("delay_seconds", 30)),
                reminder_text=str(args.get("message", "⏰ یادآوری درخواستی شما")),
            )
        if name == "propose_package_installation" and self.self_extender:
            return self.self_extender.propose_package_install(
                package_name=args["package_name"],
                version_spec=args.get("version_spec", "latest"),
                reason=args.get("reason", ""),
            )
        if name == "approve_and_install_package" and self.self_extender:
            return self.self_extender.approve_and_install_package(
                request_id=str(args.get("request_id", "")),
                confirmed_by_owner=bool(args.get("confirmed_by_owner", False)),
            )
        if name == "list_pending_package_requests" and self.self_extender:
            return {"status": "ok", "requests": self.self_extender.list_pending_requests()}
        if name == "save_code_extension" and self.self_extender:
            result = self.self_extender.save_code_extension(
                name=str(args.get("name", "")),
                code=str(args.get("code", "")),
                description=str(args.get("description", "")),
            )
            if result.get("status") == "ok":
                loaded = load_extensions(self)
                result["loaded_now"] = loaded
                if result.get("name") not in loaded:
                    # The module failed to load (e.g. a tool schema with the
                    # wrong shape) — say so clearly, WITH the correct
                    # template, instead of claiming success while the tool
                    # never went live.
                    return {
                        "status": "error",
                        "error": (
                            f"افزونه {result.get('name', '')} ذخیره شد ولی بارگذاری نشد؛ "
                            "اسکیمای ابزار داخل آن قالب درست را ندارد. "
                            "قالب درست فقط این است: {\"type\": \"function\", \"function\": {\"name\": \"<نام ابزار>\", \"description\": \"<چه کار می‌کند>\", \"parameters\": {\"type\": \"object\", \"properties\": {...}, \"required\": [...]}}}. "
                            "تابع را فقط به عنوان آرگومان سوم register_extension_tool بده و هرگز داخل اسکیما نگذار. "
                            "لاگ را با read_recent_logs بررسی کن، اسکیما را با همین قالب اصلاح کن و دوباره ذخیره کن."
                        ),
                        "path": result.get("path", ""),
                    }
                # Send the extension file to the owner so it can be added to
                # the repo — otherwise the next redeploy wipes it.
                await self._send_file_to_owner(
                    file_path=result.get("path", ""),
                    caption=f"افزونه {result.get('name', '')} — برای ماندگار شدن به ریپو اضافه‌اش کن.",
                )
            return result
        if name == "list_code_extensions" and self.self_extender:
            return {"status": "ok", "extensions": self.self_extender.list_code_extensions()}
        if name == "list_telegram_dialogs" and self.user_connector:
            return await self.user_connector.list_telegram_dialogs(limit=int(args.get("limit", 20)))
        if name == "read_telegram_messages" and self.user_connector:
            return await self.user_connector.read_telegram_messages(
                peer=str(args.get("peer", "")), limit=int(args.get("limit", 10))
            )
        if name == "run_self_check":
            return await self._run_self_check()
        if name == "read_recent_logs":
            return self._read_recent_logs(lines=int(args.get("lines", 50)))

        if name in self.extension_tools:
            handler = self.extension_tools[name]
            try:
                result = handler(args)
                if hasattr(result, "__await__"):
                    result = await result
                return result if isinstance(result, dict) else {"status": "ok", "result": result}
            except Exception as exc:
                return {"status": "error", "error": str(exc)}

        return {"status": "unsupported", "error": f"Tool '{name}' is not registered."}

    async def _run_self_check(self) -> Dict[str, Any]:
        """Health check used by the owner on demand and by the background
        self-check job: IG session file, Telethon connection, resources."""
        problems: List[str] = []
        ig_path = Path(str(settings.IG_SESSION_PATH))
        ig_ok = ig_path.exists() and ig_path.stat().st_size > 0
        if not ig_ok:
            problems.append("فایل سشن اینستاگرام پیدا نشد؛ اینستاگرام وصل نیست.")
        tg_ok = False
        if self.user_connector is not None:
            try:
                tg_ok = bool(getattr(self.user_connector, "is_connected", False))
            except Exception:
                tg_ok = False
        if not tg_ok:
            problems.append("حساب کاربری تلگرام (یوزربات) متصل نیست.")
        resources = get_vps_resource_snapshot()
        return {
            "status": "ok" if not problems else "problems_found",
            "instagram_session_ok": ig_ok,
            "telegram_user_connected": tg_ok,
            "problems": problems,
            "resources": resources,
        }

    def _read_recent_logs(self, lines: int = 50) -> Dict[str, Any]:
        log_path = Path(str(settings.LOG_FILE_PATH))
        if not log_path.exists():
            return {"status": "error", "error": "فایل لاگ پیدا نشد."}
        try:
            all_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
        tail = all_lines[-max(1, min(200, int(lines))):]
        return {"status": "ok", "lines": tail}
