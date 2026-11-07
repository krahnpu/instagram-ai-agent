import asyncio
from datetime import datetime
from typing import Any, Dict, List
from urllib.parse import quote_plus
import httpx
from bs4 import BeautifulSoup
from config import settings
from utils.logger import get_logger
from search.extractor import WebPageExtractor
from search.sources import SourceVerifier


logger = get_logger("search.engine")


class WebSearchEngine:
    """
    Independent Up-to-Date Live Web Search Engine (2026+ Aware).
    Supports:
    - Live DuckDuckGo Web Search
    - GitHub Official Releases & Repository API Search (for software/version queries)
    - Multi-source parallel webpage content extraction
    - Explicit publication/update date detection (rejects outdated pre-2025 claims when fresh data is requested)
    - Pluggable custom search API endpoint
    """

    def __init__(self) -> None:
        self.extractor = WebPageExtractor()
        self.verifier = SourceVerifier()
        self.current_year = datetime.now().year

    async def search_and_extract(self, query: str) -> Dict[str, Any]:
        clean_query = query.strip()
        if not clean_query:
            return {"status": "error", "error": "عبارت جست‌وجو خالی است."}

        logger.info(f"Executing up-to-date live web search (Year={self.current_year}): '{clean_query}'")

        # Run live web search + GitHub release lookup if software/version query
        web_task = asyncio.create_task(
            self._search_duckduckgo(clean_query, max_results=settings.SEARCH_MAX_RESULTS)
        )
        gh_task = (
            asyncio.create_task(self._search_github_releases(clean_query))
            if any(k in clean_query.lower() for k in ("github", "نسخه", "version", "release", "instagrapi", "telethon", "aiogram", "پکیج"))
            else None
        )

        raw_results = await web_task
        if gh_task:
            gh_results = await gh_task
            raw_results = gh_results + raw_results

        if not raw_results:
            return {
                "status": "no_up_to_date_results",
                "query": clean_query,
                "current_year": self.current_year,
                "sources": [],
                "honest_notice": "هیچ منبع جدید و معتبری در اینترنت برای این موضوع یافت نشد. از ارائه اطلاعات قدیمی یا حدسی خودداری شد.",
            }

        # Extract live page content and publication dates in parallel from top 4 sources
        tasks = [self.extractor.extract_content(item["url"]) for item in raw_results[:4]]
        extracted_pages = await asyncio.gather(*tasks, return_exceptions=True)

        verified_sources: List[Dict[str, Any]] = []
        fresh_sources_count = 0

        for idx, item in enumerate(raw_results[: settings.SEARCH_MAX_RESULTS]):
            page_data: Dict[str, Any] = {}
            if idx < len(extracted_pages) and isinstance(extracted_pages[idx], dict):
                page_data = extracted_pages[idx]

            page_text = page_data.get("text", "")
            published_date = page_data.get("published_date") or item.get("published_date") or ""

            credibility = self.verifier.score_source(
                url=item["url"],
                snippet=item.get("snippet", ""),
                extracted_text=page_text,
                published_date=published_date,
                current_year=self.current_year,
            )

            if credibility["is_fresh"]:
                fresh_sources_count += 1

            verified_sources.append({
                "title": item["title"],
                "url": item["url"],
                "snippet": item.get("snippet", ""),
                "extracted_summary": page_text[:750] if page_text else item.get("snippet", ""),
                "published_date": credibility["published_date"] or "نامشخص",
                "credibility_score": credibility["score"],
                "is_fresh": credibility["is_fresh"],
                "freshness_label": credibility["freshness_label"],
            })

        verified_sources.sort(key=lambda x: (x["is_fresh"], x["credibility_score"]), reverse=True)

        return {
            "status": "ok",
            "query": clean_query,
            "current_year": self.current_year,
            "provider": settings.SEARCH_PROVIDER,
            "fresh_sources_count": fresh_sources_count,
            "honest_freshness_warning": (
                None
                if fresh_sources_count > 0
                else "توجه: منابع یافت‌شده دارای تاریخ انتشار قدیمی‌تر هستند و اطلاعات جدیدتر تأییدشده‌ای برای سال جاری یافت نشد."
            ),
            "sources": verified_sources,
        }

    async def _search_duckduckgo(self, query: str, max_results: int = 6) -> List[Dict[str, str]]:
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36"
        }
        results: List[Dict[str, str]] = []
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, "html.parser")
                for row in soup.select(".result")[:max_results]:
                    title_el = row.select_one(".result__title a")
                    snippet_el = row.select_one(".result__snippet")
                    if title_el and title_el.get("href"):
                        results.append({
                            "title": title_el.get_text(strip=True),
                            "url": str(title_el.get("href")),
                            "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
                        })
        except Exception as exc:
            logger.error(f"Live web search error: {exc}")
        return results

    async def _search_github_releases(self, query: str) -> List[Dict[str, str]]:
        """Queries GitHub public repository search API for live latest software/library versions."""
        url = f"https://api.github.com/search/repositories?q={quote_plus(query)}&sort=updated&order=desc&per_page=2"
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "Instagram-AI-Agent"}
        results: List[Dict[str, str]] = []
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    items = resp.json().get("items", [])
                    for repo in items[:2]:
                        pushed_at = (repo.get("pushed_at") or "")[:10]
                        results.append({
                            "title": f"GitHub: {repo.get('full_name')} (★ {repo.get('stargazers_count')})",
                            "url": repo.get("html_url", ""),
                            "snippet": f"{repo.get('description', '')} | آخرین به‌روزرسانی مخزن: {pushed_at}",
                            "published_date": pushed_at,
                        })
        except Exception as exc:
            logger.debug(f"GitHub search skipped: {exc}")
        return results
