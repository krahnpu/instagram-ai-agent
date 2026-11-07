import difflib
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional
import httpx
from PIL import Image
import imagehash
from memory.database import DatabaseManager
from utils.logger import get_logger


logger = get_logger("content.duplicate_detector")


class DuplicateDetector:
    """
    Detects duplicate or near-duplicate Instagram posts using:
    1. Perceptual Hash (pHash) on post thumbnails / local images
    2. Keyframe pHash extraction for videos
    3. Caption semantic/character similarity ratio
    Separates 'Exact Duplicates' from 'Suspicious Duplicates' and designates which copy to keep.
    """

    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    def compute_file_phash(self, file_path: Path) -> Optional[str]:
        try:
            with Image.open(file_path) as img:
                return str(imagehash.phash(img))
        except Exception as exc:
            logger.debug(f"Could not compute pHash for {file_path}: {exc}")
            return None

    async def compute_url_phash(self, image_url: str) -> Optional[str]:
        if not image_url:
            return None
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(image_url)
                resp.raise_for_status()
                with Image.open(BytesIO(resp.content)) as img:
                    return str(imagehash.phash(img))
        except Exception:
            return None

    async def analyze_instagram_posts(self, posts: List[Dict[str, Any]]) -> Dict[str, Any]:
        exact_duplicates: List[Dict[str, Any]] = []
        suspicious_duplicates: List[Dict[str, Any]] = []

        # Enrich with pHash where thumbnail is available
        enriched: List[Dict[str, Any]] = []
        for p in posts:
            phash = await self.compute_url_phash(p.get("thumbnail_url", ""))
            enriched.append({**p, "phash": phash})

        visited = set()
        for i in range(len(enriched)):
            for j in range(i + 1, len(enriched)):
                p1, p2 = enriched[i], enriched[j]
                pair_key = tuple(sorted([p1["pk"], p2["pk"]]))
                if pair_key in visited:
                    continue

                hash_dist = 999
                if p1["phash"] and p2["phash"]:
                    h1 = imagehash.hex_to_hash(p1["phash"])
                    h2 = imagehash.hex_to_hash(p2["phash"])
                    hash_dist = h1 - h2

                cap1 = (p1.get("caption") or "").strip()
                cap2 = (p2.get("caption") or "").strip()
                cap_sim = difflib.SequenceMatcher(None, cap1, cap2).ratio() if (cap1 and cap2) else 0.0

                # Decide which post to keep (keep higher likes/engagement)
                keep, remove = (p1, p2) if (p1.get("likes", 0) >= p2.get("likes", 0)) else (p2, p1)

                if hash_dist <= 2 or (cap_sim >= 0.96 and len(cap1) > 20):
                    visited.add(pair_key)
                    exact_duplicates.append({
                        "keep_pk": keep["pk"],
                        "keep_url": keep["url"],
                        "remove_pk": remove["pk"],
                        "remove_url": remove["url"],
                        "phash_distance": hash_dist if hash_dist != 999 else "N/A",
                        "caption_similarity": round(cap_sim * 100, 1),
                        "reason": "تطابق کامل تصویر (pHash) یا کپشن یکسان",
                    })
                elif hash_dist <= 8 or cap_sim >= 0.78:
                    visited.add(pair_key)
                    suspicious_duplicates.append({
                        "keep_pk": keep["pk"],
                        "keep_url": keep["url"],
                        "suspect_pk": remove["pk"],
                        "suspect_url": remove["url"],
                        "phash_distance": hash_dist if hash_dist != 999 else "N/A",
                        "caption_similarity": round(cap_sim * 100, 1),
                        "reason": "شباهت بالای بصری یا کپشن مشابه (مورد مشکوک)",
                    })

        return {
            "exact_duplicates": exact_duplicates,
            "suspicious_duplicates": suspicious_duplicates,
        }
