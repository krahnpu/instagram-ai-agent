import asyncio
import re
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


def _normalize_persian(text: str) -> str:
    """Unify Arabic/Persian letter variants so «امریکا» matches «آمریکا»."""
    return (
        text.replace("آ", "ا").replace("أ", "ا").replace("إ", "ا")
        .replace("ة", "ه").replace("ؤ", "و").replace("ي", "ی").replace("ك", "ک")
        .replace("ـ", "")
    )


def _query_tokens(query: str) -> List[str]:
    """Meaningful word tokens of a query (Persian/Arabic or Latin), used to
    check whether a search result is actually about the question."""
    tokens = re.findall(r"[\u0600-\u06FFa-zA-Z0-9]{2,}", _normalize_persian(query))
    return [t for t in tokens if len(t) >= 3 or (t.isascii() and len(t) >= 2)]


def _result_is_relevant(result: Dict[str, str], query_tokens: List[str]) -> bool:
    text = _normalize_persian(f"{result.get('title', '')} {result.get('snippet', '')}").lower()
    return any(tok.lower() in text for tok in query_tokens)


def clean_search_query(query: str) -> str:
    """Turn a natural-language question into a search phrase: drop trailing
    question marks and collapse whitespace."""
    q = query.strip().rstrip("؟?").strip()
    return re.sub(r"\s+", " ", q)


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
        clean_query = clean_search_query(query)
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

        raw_results = []
        try:
            raw_results = await web_task
        except Exception as exc:
            logger.warning(f"DuckDuckGo search failed, trying Bing fallback: {exc}")
        if gh_task:
            try:
                gh_results = await gh_task
                raw_results = gh_results + raw_results
            except Exception as exc:
                logger.warning(f"GitHub releases search failed: {exc}")
        # Relevance gate (also applied to Bing results below): search engines
        # sometimes return junk unrelated to the question (e.g. "Montreal
        # Canadiens" for a Persian news query). Drop results sharing no word
        # with the query instead of letting the agent answer from them — no
        # relevant source is better than a wrong one.
        raw_results = self._relevant_results(raw_results, clean_query)
        if not raw_results:
            # DuckDuckGo often returns nothing (or only junk) from datacenter
            # IPs — fall back to Bing so a search question still gets an answer.
            try:
                bing_results = await self._search_bing(clean_query, max_results=settings.SEARCH_MAX_RESULTS)
                raw_results = self._relevant_results(bing_results, clean_query)
            except Exception as exc:
                logger.warning(f"Bing fallback search failed: {exc}")
                raw_results = []

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

    def _relevant_results(self, results: List[Dict[str, str]], query: str) -> List[Dict[str, str]]:
        query_tokens = _query_tokens(query)
        if not query_tokens:
            return results
        relevant = [r for r in results if _result_is_relevant(r, query_tokens)]
        if relevant:
            return relevant
        logger.warning(f"Search results for '{query}' were all irrelevant; treating as no results.")
        return []

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

    async def _search_bing(self, query: str, max_results: int = 6) -> List[Dict[str, str]]:
        """Fallback when DuckDuckGo returns nothing (common from datacenter IPs)."""
        url = f"https://www.bing.com/search?q={quote_plus(query)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36"
        }
        results: List[Dict[str, str]] = []
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, "html.parser")
                for row in soup.select(".b_algo")[:max_results]:
                    link_el = row.select_one("h2 a")
                    snippet_el = row.select_one(".b_caption p") or row.select_one("p")
                    href = str(link_el.get("href")) if link_el and link_el.get("href") else ""
                    if link_el and href.startswith("http"):
                        results.append({
                            "title": link_el.get_text(strip=True),
                            "url": href,
                            "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
                        })
        except Exception as exc:
            logger.error(f"Bing fallback search error: {exc}")
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
