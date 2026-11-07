import re
from typing import Any, Dict
import httpx
from bs4 import BeautifulSoup
from utils.logger import get_logger


logger = get_logger("search.extractor")


class WebPageExtractor:
    """Extracts clean readable text AND publication/modification dates from web pages."""

    DATE_META_KEYS = (
        "article:published_time",
        "article:modified_time",
        "og:updated_time",
        "pubdate",
        "publishdate",
        "date",
        "dc.date",
    )

    async def extract_content(self, url: str) -> Dict[str, Any]:
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36"
        }
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, "html.parser")

                published_date = self._extract_date(soup)

                for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
                    tag.decompose()

                paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all(["p", "article", "h1", "h2", "li"])]
                clean_text = " ".join([p for p in paragraphs if len(p) > 25])
                return {
                    "url": url,
                    "text": clean_text[:2500],
                    "published_date": published_date,
                }
        except Exception as exc:
            logger.debug(f"Extraction skipped for {url}: {exc}")
            return {"url": url, "text": "", "published_date": ""}

    def _extract_date(self, soup: BeautifulSoup) -> str:
        # 1. Check <time datetime="...">
        time_tag = soup.find("time")
        if time_tag:
            dt_val = time_tag.get("datetime") or time_tag.get_text(strip=True)
            if dt_val:
                return str(dt_val)[:25]

        # 2. Check standard meta tags
        for meta in soup.find_all("meta"):
            prop = (meta.get("property") or meta.get("name") or "").lower()
            if prop in self.DATE_META_KEYS and meta.get("content"):
                return str(meta.get("content"))[:25]

        # 3. Regex ISO date fallback in page text
        raw_text = soup.get_text(" ", strip=True)[:3000]
        iso_match = re.search(r"\b(202[4-9]-[01]\d-[0-3]\d)\b", raw_text)
        if iso_match:
            return iso_match.group(1)

        return ""
