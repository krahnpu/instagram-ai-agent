from typing import Any, Dict, List
from core.permissions import Role, PermissionManager


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
                    "description": "Publish a video Reel to Instagram via Instagrapi clip_upload.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "video_path": {"type": "string", "description": "Local file path to MP4 video"},
                            "caption": {"type": "string", "description": "Reel caption and hashtags"},
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
                    "description": "Fetch latest video or photo from a configured Telegram channel via Telethon.",
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
        ]

        if role == Role.OWNER:
            return all_tools

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
            return await self.ig_publisher.publish_reel(args["video_path"], args.get("caption", ""))
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
        if name == "create_scheduled_task" and self.scheduler:
            return await self.scheduler.create_task_from_natural_language(args.get("instruction", ""))
        if name == "propose_package_installation" and self.self_extender:
            return self.self_extender.propose_package_install(
                package_name=args["package_name"],
                version_spec=args.get("version_spec", "latest"),
                reason=args.get("reason", ""),
            )

        return {"status": "unsupported", "error": f"Tool '{name}' is not registered."}
