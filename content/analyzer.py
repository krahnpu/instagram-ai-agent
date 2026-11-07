from pathlib import Path
from typing import Any, Dict
from PIL import Image


class ContentAnalyzer:
    """Analyzes image and video metadata (dimensions, aspect ratio, format, suitability for Reels/Posts)."""

    def inspect_media(self, file_path: str) -> Dict[str, Any]:
        path = Path(file_path)
        if not path.exists():
            return {"valid": False, "error": "فایل رسانه وجود ندارد."}

        size_mb = round(path.stat().st_size / (1024 * 1024), 2)
        ext = path.suffix.lower()

        if ext in (".jpg", ".jpeg", ".png", ".webp"):
            try:
                with Image.open(path) as img:
                    width, height = img.size
                    ratio = round(width / max(1, height), 2)
                    return {
                        "valid": True,
                        "media_type": "photo",
                        "width": width,
                        "height": height,
                        "aspect_ratio": ratio,
                        "size_mb": size_mb,
                    }
            except Exception as exc:
                return {"valid": False, "error": str(exc)}

        if ext in (".mp4", ".mov", ".mkv"):
            return {
                "valid": True,
                "media_type": "video",
                "recommended_format": "reel" if size_mb < 150 else "video",
                "size_mb": size_mb,
            }

        return {"valid": False, "error": f"فرمت فایل پشتیبانی نمی‌شود: {ext}"}
