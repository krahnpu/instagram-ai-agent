import subprocess
from pathlib import Path
from typing import Any, Dict, Optional
from config import settings
from utils.logger import get_logger


logger = get_logger("content.media_processor")


class MediaProcessor:
    """
    Lightweight media processor optimized for 2GB RAM / 2GB Storage Linux VPS.
    Extracts single keyframes via FFmpeg subprocess without loading heavy video
    arrays into RAM, and enforces strict temporary directory quota cleanup.
    """

    def __init__(self, temp_dir: Path) -> None:
        self.temp_dir = temp_dir
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.max_storage_bytes = settings.MAX_TEMP_STORAGE_MB * 1024 * 1024

    def _ffmpeg_exe(self) -> str:
        # Static ffmpeg binary bundled with the pip package imageio-ffmpeg —
        # works on servers (e.g. Render free) where no system ffmpeg exists
        # and apt installs are impossible. Falls back to system ffmpeg.
        try:
            import imageio_ffmpeg

            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return "ffmpeg"

    def extract_video_keyframe(self, video_path: Path) -> Optional[Path]:
        output_frame = self.temp_dir / f"{video_path.stem}_frame.jpg"
        cmd = [
            self._ffmpeg_exe(),
            "-y",
            "-ss",
            "00:00:01",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-q:v",
            "4",
            str(output_frame),
        ]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=True)
            return output_frame if output_frame.exists() else None
        except Exception as exc:
            logger.debug(f"Keyframe extraction skipped for {video_path}: {exc}")
            return None

    def enforce_storage_quota(self) -> Dict[str, Any]:
        """Evicts oldest temporary media files when temp directory exceeds MAX_TEMP_STORAGE_MB."""
        files = sorted(
            [f for f in self.temp_dir.glob("*") if f.is_file()],
            key=lambda f: f.stat().st_mtime,
        )
        total_bytes = sum(f.stat().st_size for f in files)
        removed_count = 0

        while total_bytes > self.max_storage_bytes and files:
            oldest = files.pop(0)
            size = oldest.stat().st_size
            try:
                oldest.unlink()
                total_bytes -= size
                removed_count += 1
            except OSError:
                pass

        return {
            "current_temp_mb": round(total_bytes / (1024 * 1024), 2),
            "max_temp_mb": settings.MAX_TEMP_STORAGE_MB,
            "evicted_files": removed_count,
        }
