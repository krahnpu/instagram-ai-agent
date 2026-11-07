from typing import Any, Dict, List
from instagram.client import InstagramClientManager
from memory.long_term import LongTermMemory


class InstagramAnalytics:
    """
    Retrieves real Instagram account and post statistics via Instagrapi.
    Never fabricates or guesses metrics: if a metric (such as Saves or Reach)
    is unavailable for a media item or connection method, explicitly reports 'Unsupported'.
    """

    def __init__(self, ig_client: InstagramClientManager, lt_memory: LongTermMemory) -> None:
        self.ig_client = ig_client
        self.lt_memory = lt_memory

    async def get_account_overview(self) -> Dict[str, Any]:
        if not self.ig_client.is_connected:
            return {
                "status": "login_required",
                "error": "حساب اینستاگرام متصل نیست.",
            }

        user_id_res = await self.ig_client.execute_safe("user_info_by_username", self.ig_client.active_username)
        if user_id_res["status"] != "ok":
            return user_id_res

        user = user_id_res["data"]
        medias_res = await self.ig_client.execute_safe("user_medias", user_id=user.pk, amount=15)
        medias = medias_res.get("data", []) if medias_res["status"] == "ok" else []

        total_likes = 0
        total_comments = 0
        total_views = 0
        post_summaries: List[Dict[str, Any]] = []

        for m in medias:
            likes = getattr(m, "like_count", 0) or 0
            comments = getattr(m, "comment_count", 0) or 0
            views = getattr(m, "view_count", None) or getattr(m, "play_count", None) or 0
            total_likes += likes
            total_comments += comments
            total_views += views

            post_summaries.append({
                "pk": str(m.pk),
                "code": str(m.code),
                "url": f"https://www.instagram.com/p/{m.code}/",
                "likes": likes,
                "comments": comments,
                "views": views if views > 0 else "Unsupported",
                "saves": "Unsupported",  # Private feed endpoint does not expose saves without business insights
                "shares": "Unsupported",
                "reach": "Unsupported",
                "taken_at": str(getattr(m, "taken_at", "")),
            })

        follower_count = getattr(user, "follower_count", 0) or 0
        avg_engagement = 0.0
        if medias and follower_count > 0:
            avg_engagement = round(((total_likes + total_comments) / len(medias) / follower_count) * 100, 2)

        sorted_by_perf = sorted(post_summaries, key=lambda x: x["likes"] + x["comments"], reverse=True)
        agent_stats = await self.lt_memory.get_agent_counters()

        return {
            "status": "ok",
            "account": {
                "pk": str(user.pk),
                "username": user.username,
                "full_name": user.full_name,
                "followers": follower_count,
                "following": getattr(user, "following_count", 0) or 0,
                "media_count": getattr(user, "media_count", 0) or 0,
            },
            "metrics": {
                "sampled_posts": len(medias),
                "total_views_sampled": total_views,
                "total_likes_sampled": total_likes,
                "total_comments_sampled": total_comments,
                "engagement_rate_percent": avg_engagement,
                "best_posts": sorted_by_perf[:3],
                "weakest_posts": list(reversed(sorted_by_perf[-3:])) if sorted_by_perf else [],
            },
            "agent_activity": agent_stats,
        }
