import re
from typing import Any, Dict
from urllib.parse import urlparse


class SourceVerifier:
    """
    Evaluates source credibility, publication date, and freshness relative to the current year (2026+).
    Prevents presenting outdated 2024/2025 articles as brand-new breaking news without explicit date disclosure.
    """

    TRUSTED_DOMAINS = {
        "wikipedia.org",
        "android.com",
        "developer.android.com",
        "google.com",
        "github.com",
        "pypi.org",
        "reuters.com",
        "bbc.com",
        "theverge.com",
        "arstechnica.com",
        "gsmarena.com",
        "zoomit.ir",
        "digiato.com",
    }

    def score_source(
        self,
        url: str,
        snippet: str,
        extracted_text: str,
        published_date: str = "",
        current_year: int = 2026,
    ) -> Dict[str, Any]:
        domain = urlparse(url).netloc.lower().lstrip("www.")
        score = 55

        for trusted in self.TRUSTED_DOMAINS:
            if domain.endswith(trusted):
                score += 25
                break

        combined = f"{published_date} {snippet} {extracted_text}"
        years_found = [int(y) for y in re.findall(r"\b(202[0-9])\b", combined)]
        latest_year = max(years_found) if years_found else 0

        is_current_year = latest_year >= current_year
        is_recent = latest_year >= (current_year - 1)

        if is_current_year:
            score += 20
            freshness_label = f"به‌روز ({latest_year})"
        elif is_recent:
            score += 8
            freshness_label = f"نسبتاً جدید ({latest_year})"
        elif latest_year > 0:
            score -= 10
            freshness_label = f"قدیمی ({latest_year})"
        else:
            freshness_label = "تاریخ نامشخص"

        return {
            "domain": domain,
            "score": max(10, min(100, score)),
            "is_fresh": is_current_year or is_recent,
            "published_date": published_date or (str(latest_year) if latest_year else "نامشخص"),
            "freshness_label": freshness_label,
            "detected_years": sorted(list(set(years_found)), reverse=True),
        }
