from enum import Enum
from typing import Set
from config import settings
from utils.logger import get_logger


logger = get_logger("core.permissions")


class Role(str, Enum):
    OWNER = "owner"
    REGULAR_USER = "regular_user"


class PermissionManager:
    """
    Server-side Role-Based Access Control (RBAC).
    Strictly separates Owner (TELEGRAM_OWNER_ID) from Regular Users.
    Regular users can only converse and trigger web search; they cannot access
    sessions, publish/delete posts, alter settings, or view logs.
    """

    PUBLIC_TOOLS: Set[str] = {
        "web_search",
    }

    OWNER_ONLY_TOOLS: Set[str] = {
        "get_instagram_profile",
        "list_instagram_posts",
        "publish_instagram_photo",
        "publish_instagram_video",
        "publish_instagram_reel",
        "publish_instagram_carousel",
        "publish_instagram_story",
        "scan_duplicate_posts",
        "find_low_view_posts",
        "delete_instagram_posts",
        "get_recent_direct_messages",
        "reply_direct_message",
        "get_post_comments",
        "reply_post_comment",
        "get_media_music_info",
        "fetch_telegram_channel_media",
        "save_admin_preference",
        "create_scheduled_task",
        "delete_scheduled_task",
        "pause_scheduled_task",
        "resume_scheduled_task",
        "list_scheduled_tasks",
        "send_message_via_user_account",
        "get_telegram_user_account_status",
        "create_one_off_reminder",
        "start_telegram_login",
        "submit_telegram_code",
        "submit_telegram_2fa",
        "import_instagram_session",
        "test_instagram_connection",
        "propose_package_installation",
    }

    def get_user_role(self, user_id: int) -> Role:
        if int(user_id) == int(settings.TELEGRAM_OWNER_ID) and settings.TELEGRAM_OWNER_ID != 0:
            return Role.OWNER
        return Role.REGULAR_USER

    def is_owner(self, user_id: int) -> bool:
        return self.get_user_role(user_id) == Role.OWNER

    def can_execute_tool(self, user_id: int, tool_name: str) -> bool:
        role = self.get_user_role(user_id)
        if role == Role.OWNER:
            return True
        allowed = tool_name in self.PUBLIC_TOOLS
        if not allowed:
            logger.warning(f"RBAC blocked regular user {user_id} from invoking '{tool_name}'")
        return allowed
