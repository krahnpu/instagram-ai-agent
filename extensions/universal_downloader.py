"""Universal media downloader + song finder extension for the Instagram AI agent.

Tools:
- download_media: download audio/video from any yt-dlp-supported URL
  (Instagram, YouTube, TikTok, X, SoundCloud, ...). Spotify track URLs
  fall back to a YouTube search for the same track, because yt-dlp cannot
  pull audio straight from Spotify.
- identify_song: identify the song in a local audio file (a voice note, a
  video's audio track) via Shazam.
- find_song_of_video: download a video's audio, identify the song, then
  download and send the full song.

Requires the yt-dlp and shazamio packages on the host. If one is missing,
the tool returns a clear message telling the agent to install it with
approve_and_install_package instead of failing silently.

Schemas follow the OpenAI function-calling format exactly:
{"type": "function", "function": {"name", "description", "parameters"}}.
Never put the handler function (or any Python object) inside the schema.
"""

import asyncio
import httpx
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

_REGISTRY = None
_MAX_SEND_BYTES = 48 * 1024 * 1024  # Telegram Bot API file limit is 50 MB


# ---------------------------------------------------------------- helpers

def _media_dir() -> Path:
    try:
        from config import settings
        d = Path(settings.TEMP_MEDIA_DIR)
    except Exception:
        d = Path("data/temp_media")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ytdlp_cmd_base():
    """Return the command prefix for yt-dlp, or None if not installed."""
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    # pip installs the module too; `python -m yt_dlp` works when the
    # console script is not on PATH.
    try:
        import yt_dlp  # noqa: F401
        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        return None


def _ytdlp_available() -> bool:
    base = _ytdlp_cmd_base()
    if not base:
        return False
    try:
        proc = subprocess.run(base + ["--version"], capture_output=True, text=True, timeout=30)
        return proc.returncode == 0
    except Exception:
        return False


def _ffmpeg_location() -> Optional[str]:
    """Path of the static ffmpeg binary bundled with imageio-ffmpeg.
    Render's free image has no ffmpeg/ffprobe on PATH, so yt-dlp audio
    extraction (-x) and stream merging (bv+ba) fail with
    'ffprobe and ffmpeg not found' unless yt-dlp is told where ffmpeg is."""
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        return p if p and Path(p).exists() else None
    except Exception:
        return None


def _run_ytdlp(args, timeout=180):
    base = _ytdlp_cmd_base()
    if not base:
        return None
    ff = _ffmpeg_location()
    # YouTube throttles/blocks the default web client from datacenter IPs
    # (Render): the android player client gets full-speed, unblocked streams,
    # and --socket-timeout fails fast on stalled connections instead of
    # hanging until the subprocess timeout.
    full = base + (["--ffmpeg-location", ff] if ff else []) + [
        "--socket-timeout", "30",
        "--extractor-args", "youtube:player_client=android",
    ] + args
    return subprocess.run(full, capture_output=True, text=True, timeout=timeout)


def _ytdlp_json(url: str) -> Optional[Dict[str, Any]]:
    """Fetch metadata only (no download). Returns None on failure."""
    try:
        proc = _run_ytdlp(["--dump-json", "--no-playlist", "--no-download", url], timeout=120)
    except Exception:
        return None
    if not proc or proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout.strip().splitlines()[0])
    except Exception:
        return None


def _search_candidates(search_url: str, limit: int = 3) -> list[str]:
    """Resolve a yt-dlp search URL (ytsearchN:/scsearchN:) to candidate page
    URLs, without downloading. Returns [] when the search finds nothing.
    The caller tries each candidate until one downloads — the first result
    is often DRM-protected, geo-blocked or deleted (e.g. SoundCloud's top
    Latin result for a Persian song)."""
    try:
        proc = _run_ytdlp(["--dump-json", "--flat-playlist", "--no-playlist",
                           "--no-download", search_url], timeout=120)
    except Exception:
        return []
    if not proc or proc.returncode != 0:
        return []
    out: list[str] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        u = item.get("webpage_url") or item.get("url") or ""
        if u and u not in out:
            out.append(u)
        if len(out) >= limit:
            break
    return out


def _is_search_url(url: str) -> bool:
    return url.startswith(("ytsearch", "scsearch", "ytsearch1:", "scsearch1:"))


def _search_urls_for(url: str, kind: str) -> list[str]:
    """Ordered search URLs to try for one audio download request.
    SoundCloud first, YouTube second (user's choice)."""
    if kind != "audio":
        return [url]
    if url.startswith("ytsearch1:"):
        q = url[len("ytsearch1:"):]
        return [f"scsearch1:{q}", url]
    if url.startswith("scsearch1:"):
        q = url[len("scsearch1:"):]
        return [url, f"ytsearch1:{q}"]
    return [url]


async def _download_and_send(url: str, kind: str, out_dir: Path, note: str = "") -> Dict[str, Any]:
    """Download one URL with yt-dlp and send the file to the owner."""
    job_dir = out_dir / f"dl_{uuid.uuid4().hex[:10]}"
    job_dir.mkdir(parents=True, exist_ok=True)
    template = str(job_dir / "%(title).60s.%(ext)s")
    if kind == "audio":
        base_args = ["-x", "--audio-format", "mp3", "--audio-quality", "0",
                     "--no-playlist", "-o", template]
    else:
        base_args = ["-f", "bv*[height<=720]+ba/b[height<=720]/b",
                     "--max-filesize", "45M", "--no-playlist", "-o", template]
    # Search URLs resolve to several candidates; try each until one downloads.
    last_err = ""
    proc = None
    for search_url in _search_urls_for(url, kind):
        cands = _search_candidates(search_url) if _is_search_url(search_url) else [search_url]
        if not cands:
            last_err = "جستجو نتیجه‌ای نداشت"
            continue
        for cand in cands:
            try:
                proc = await asyncio.to_thread(_run_ytdlp, base_args + [cand], 120)
            except subprocess.TimeoutExpired:
                last_err = "دانلود طول کشید و متوقف شد"
                proc = None
                continue
            except Exception as exc:
                last_err = f"خطا در اجرای دانلود: {exc}"
                proc = None
                continue
            if proc and proc.returncode == 0:
                break
            last_err = ((proc.stderr or proc.stdout or "")[-500:] if proc else "")
            proc = None
        if proc and proc.returncode == 0:
            break
    if not proc or proc.returncode != 0:
        return {"status": "error", "error": f"دانلود ناموفق بود: {last_err}"}
    files = [p for p in job_dir.iterdir() if p.is_file()]
    if not files:
        return {"status": "error", "error": "دانلود تمام شد ولی فایلی ساخته نشد."}
    target = max(files, key=lambda p: p.stat().st_size)
    size_mb = target.stat().st_size / (1024 * 1024)
    if target.stat().st_size > _MAX_SEND_BYTES:
        return {"status": "error", "error": f"فایل خیلی بزرگ است ({size_mb:.1f} مگابایت)؛ تلگرام بیشتر از ۵۰ مگابایت نمی‌فرستد."}
    if _REGISTRY is not None:
        sent = await _REGISTRY._send_file_to_owner(file_path=str(target), caption=target.name)
        if sent.get("status") == "error":
            return {"status": "error", "error": f"دانلود شد ولی ارسال نشد: {sent.get('error')}"}
    msg = f"دانلود شد و فرستاده شد: {target.name} ({size_mb:.1f} مگابایت)"
    if note:
        msg = f"{note}\n{msg}"
    return {"status": "ok", "message": msg, "file": target.name, "size_mb": round(size_mb, 1)}


async def _to_wav(path: Path) -> Path:
    """Convert an audio file to WAV (for Shazam) if needed. Returns the path to use."""
    if path.suffix.lower() in {".mp3", ".wav", ".m4a", ".flac", ".aac"}:
        return path
    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return path
    out = path.with_suffix(".wav")
    try:
        await asyncio.to_thread(
            subprocess.run,
            [ffmpeg, "-y", "-i", str(path), "-ar", "44100", "-ac", "1", str(out)],
            capture_output=True, timeout=120,
        )
    except Exception:
        return path
    return out if out.exists() and out.stat().st_size > 0 else path


# ---------------------------------------------------------------- tools

def _spotify_title(url: str) -> Optional[str]:
    """Get a Spotify track's title from Spotify's public oEmbed endpoint
    (no auth needed). yt-dlp 2026.10.26 has no Spotify extractor, so its
    metadata fetch always fails with a DRM error — oEmbed is the way to
    learn the track name from a Spotify link. Returns None on failure."""
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            resp = client.get("https://open.spotify.com/oembed",
                              params={"url": url})
            resp.raise_for_status()
            title = (resp.json().get("title") or "").strip()
            return title or None
    except Exception:
        return None


async def download_media(args: Dict[str, Any]) -> Dict[str, Any]:
    url = (args.get("url") or "").strip()
    kind = (args.get("kind") or "auto").strip().lower()
    if not url:
        return {"status": "error", "error": "لینک را نفرستادی؛ یک URL بده."}
    if not _ytdlp_available():
        return {"status": "error",
                "error": "yt-dlp نصب نیست. با ابزار approve_and_install_package پکیج yt-dlp را نصب کن و دوباره تلاش کن."}
    out_dir = _media_dir()
    # Spotify serves metadata only through yt-dlp, no audio — and yt-dlp
    # 2026.10.26 has no Spotify extractor at all, so its metadata fetch
    # always fails with a DRM error. Learn the track name from Spotify's
    # public oEmbed endpoint instead, then find the same track on SoundCloud
    # or YouTube. Try several query forms in order — the exact
    # "artist - title" is not always findable.
    if "open.spotify.com" in url or url.startswith("spotify:"):
        meta = await asyncio.to_thread(_ytdlp_json, url)
        artist = ""
        title = ""
        if meta:
            artist = (meta.get("artist") or meta.get("uploader") or "").strip()
            title = (meta.get("title") or "").strip()
        if not title:
            title = await asyncio.to_thread(_spotify_title, url) or ""
        if not title:
            return {"status": "error", "error": "نتوانستم اطلاعات آهنگ اسپاتیفای را بخوانم."}
        queries: list[str] = []
        if artist and title:
            queries.append(f"{artist} - {title}")
        if title:
            queries.append(title)
        if artist and title:
            queries.append(f"{title} {artist}")
        last_err = ""
        for q in queries:
            dl = await _download_and_send(
                f"ytsearch1:{q}", "audio", out_dir,
                note=f"اسپاتیفای صدا را مستقیم نمی‌دهد؛ همین آهنگ را از ساندکلاد یا یوتیوب پیدا کردم: {q}",
            )
            if dl.get("status") != "error":
                return dl
            last_err = dl.get("error", "")
        return {"status": "error", "error": f"آهنگ اسپاتیفای پیدا نشد: {last_err}"}
    if kind == "audio":
        return await _download_and_send(url, "audio", out_dir)
    return await _download_and_send(url, "video", out_dir)


async def identify_song(args: Dict[str, Any]) -> Dict[str, Any]:
    path_str = (args.get("file_path") or "").strip()
    if not path_str or not Path(path_str).exists():
        return {"status": "error", "error": "فایل صوتی پیدا نشد؛ مسیر درست فایل را بده."}
    try:
        import shazamio
    except ImportError:
        return {"status": "error",
                "error": "پکیج shazamio نصب نیست. با ابزار approve_and_install_package پکیج shazamio را نصب کن و دوباره تلاش کن."}
    path = await _to_wav(Path(path_str))
    shazam = None
    try:
        shazam = shazamio.Shazam()
        # shazamio 0.9+ renamed recognize_song to recognize(data), where data
        # is a path, bytes or bytearray. The old name no longer exists.
        res = await shazam.recognize(str(path))
    except Exception as exc:
        return {"status": "error", "error": f"تشخیص آهنگ ناموفق بود: {exc}"}
    finally:
        if shazam is not None:
            try:
                await shazam.close()
            except Exception:
                pass
    track = (res or {}).get("track") or {}
    if not track:
        matches = (res or {}).get("matches") or []
        if matches and isinstance(matches[0], dict):
            track = matches[0].get("track") or {}
    if not track:
        return {"status": "ok", "found": False,
                "message": "آهنگی پیدا نشد؛ شاید صدا واضح نیست یا این صدا آهنگ نیست."}
    title = track.get("title") or ""
    artist = track.get("subtitle") or ""
    link = track.get("url") or ""
    return {"status": "ok", "found": True, "title": title, "artist": artist, "url": link,
            "message": f"این آهنگ است: {artist} - {title}"}


async def find_song_of_video(args: Dict[str, Any]) -> Dict[str, Any]:
    url = (args.get("url") or "").strip()
    query_fa = (args.get("query_fa") or "").strip()
    send_full = bool(args.get("send_full_song", True))
    if not url:
        return {"status": "error", "error": "لینک ویدئو را نفرستادی؛ یک URL بده."}
    if not _ytdlp_available():
        return {"status": "error",
                "error": "yt-dlp نصب نیست. با ابزار approve_and_install_package پکیج yt-dlp را نصب کن و دوباره تلاش کن."}
    out_dir = _media_dir()
    job_dir = out_dir / f"aud_{uuid.uuid4().hex[:10]}"
    job_dir.mkdir(parents=True, exist_ok=True)
    template = str(job_dir / "%(title).60s.%(ext)s")
    try:
        proc = await asyncio.to_thread(
            _run_ytdlp,
            ["-x", "--audio-format", "mp3", "--audio-quality", "0",
             "--no-playlist", "-o", template, url], 300,
        )
    except Exception as exc:
        return {"status": "error", "error": f"خطا در گرفتن صدای ویدئو: {exc}"}
    if not proc or proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "")[-400:] if proc else ""
        return {"status": "error", "error": f"نتوانستم صدای ویدئو را بگیرم: {err}"}
    files = [p for p in job_dir.iterdir() if p.is_file()]
    if not files:
        return {"status": "error", "error": "صدای ویدئو دانلود نشد."}
    audio_path = max(files, key=lambda p: p.stat().st_size)
    ident = await identify_song({"file_path": str(audio_path)})
    if not ident.get("found"):
        return {"status": "ok", "found": False,
                "message": "آهنگ این ویدئو پیدا نشد؛ شاید ویدئو آهنگ مشخصی ندارد."}
    msg = f"آهنگ این ویدئو: {ident.get('artist', '')} - {ident.get('title', '')}"
    if not send_full:
        return {"status": "ok", "found": True, "title": ident.get("title"),
                "artist": ident.get("artist"), "url": ident.get("url"), "message": msg}
    query = f"{ident.get('artist', '')} - {ident.get('title', '')}".strip(" -")
    # Persian songs are often only findable with the Persian spelling
    # (SoundCloud/YouTube titles are in Persian script), so try the Persian
    # query first, then the Latin one. Each query tries SoundCloud then
    # YouTube. Never fall back to the extracted clip — if the full song is
    # not found, say so; the owner does not want the short clip.
    queries = [q for q in (query_fa, query) if q]
    last_err = ""
    for q in queries:
        dl = await _download_and_send(f"ytsearch1:{q}", "audio", out_dir,
                                      note=f"{msg}\nآهنگ کامل:")
        if dl.get("status") != "error":
            return {"status": "ok", "found": True, "title": ident.get("title"),
                    "artist": ident.get("artist"), "url": ident.get("url"),
                    "message": f"{msg}\nآهنگ کامل هم فرستاده شد."}
        last_err = dl.get("error", "")
    return {"status": "ok", "found": True, "title": ident.get("title"),
            "artist": ident.get("artist"), "url": ident.get("url"),
            "message": f"{msg}\n(آهنگ کامل دانلود نشد: {last_err})"}


async def extract_audio(args: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the audio track of a LOCAL video file and send it as mp3.
    This is what the owner means by "خود آهنگ را بفرست" for a video they
    already sent — no internet download involved."""
    path_str = (args.get("video_path") or "").strip()
    if not path_str or not Path(path_str).exists():
        return {"status": "error", "error": "فایل ویدئو پیدا نشد؛ مسیر درست فایل را بده."}
    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return {"status": "error", "error": "ffmpeg در دسترس نیست؛ نمی‌توانم صدا را استخراج کنم."}
    src = Path(path_str)
    out = _media_dir() / f"{src.stem}.mp3"
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            [ffmpeg, "-y", "-i", str(src), "-vn", "-codec:a", "libmp3lame", "-q:a", "2", str(out)],
            capture_output=True, timeout=300,
        )
    except Exception as exc:
        return {"status": "error", "error": f"استخراج صدا ناموفق بود: {exc}"}
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        err = (proc.stderr or b"")[-300:].decode("utf-8", "replace")
        return {"status": "error", "error": f"استخراج صدا ناموفق بود: {err}"}
    size_mb = out.stat().st_size / (1024 * 1024)
    if out.stat().st_size > _MAX_SEND_BYTES:
        return {"status": "error", "error": f"فایل صوتی خیلی بزرگ است ({size_mb:.1f} مگابایت)؛ تلگرام بیشتر از ۵۰ مگابایت نمی‌فرستد."}
    if _REGISTRY is not None:
        sent = await _REGISTRY._send_file_to_owner(file_path=str(out), caption=out.name)
        if sent.get("status") == "error":
            return {"status": "error", "error": f"صدا استخراج شد ولی ارسال نشد: {sent.get('error')}"}
    return {"status": "ok", "message": f"صدای ویدئو استخراج و فرستاده شد: {out.name} ({size_mb:.1f} مگابایت)",
            "file": out.name, "size_mb": round(size_mb, 1)}


# ---------------------------------------------------------------- registration

def register(registry):
    global _REGISTRY
    _REGISTRY = registry
    registry.register_extension_tool(
        "download_media",
        {"type": "function", "function": {
            "name": "download_media",
            "description": "Download audio or video from any URL yt-dlp supports (Instagram, YouTube, TikTok, X, SoundCloud and more) and send the file to the owner. Spotify track links fall back to the same track found on YouTube. Do not try shazam.com links — they return 403 from servers; search for the song by title instead. For a song title, pass the title as a search query: the tool tries SoundCloud first, then YouTube, and tries several search results until one downloads. If the Latin spelling fails, retry with the Persian spelling of the title.",
            "parameters": {"type": "object", "properties": {
                "url": {"type": "string", "description": "The page or media URL to download, or a search query for a song (the tool searches SoundCloud first, then YouTube)."},
                "kind": {"type": "string", "description": "video, audio, or auto (default)."},
            }, "required": ["url"]},
        }},
        download_media,
    )
    registry.register_extension_tool(
        "identify_song",
        {"type": "function", "function": {
            "name": "identify_song",
            "description": "Identify the song in a local audio file (a voice note, a video audio track) using Shazam. Give the local file path.",
            "parameters": {"type": "object", "properties": {
                "file_path": {"type": "string", "description": "Local path of the audio file to identify."},
            }, "required": ["file_path"]},
        }},
        identify_song,
    )
    registry.register_extension_tool(
        "extract_audio",
        {"type": "function", "function": {
            "name": "extract_audio",
            "description": "Extract the audio track of a LOCAL video file (already on the server) and send it to the owner as an mp3. ONLY use this when the owner explicitly asks for the audio of that video itself (for example 'صدای همین ویدیو را بفرست'). NEVER use it when the owner asked for the full song — in that case download the full song from the internet with download_media or find_song_of_video instead, and if the full song is not found say so instead of sending the extracted short clip.",
            "parameters": {"type": "object", "properties": {
                "video_path": {"type": "string", "description": "Local file path of the video."},
            }, "required": ["video_path"]},
        }},
        extract_audio,
    )
    registry.register_extension_tool(
        "find_song_of_video",
        {"type": "function", "function": {
            "name": "find_song_of_video",
            "description": "Find the song used in a video URL: extracts the video audio, identifies the song with Shazam, and downloads and sends the FULL song from the internet (SoundCloud first, then YouTube). If you also know the song title in Persian script, pass it as query_fa — Persian titles are often only found with the Persian spelling. Never send the extracted clip from the video as a substitute for the full song; if the full song cannot be found, say so in one sentence.",
            "parameters": {"type": "object", "properties": {
                "url": {"type": "string", "description": "The video URL."},
                "query_fa": {"type": "string", "description": "The song title and artist in Persian script, if you know it (for example 'گل بی گلدون گوگوش'). Tried first, before the Latin title."},
                "send_full_song": {"type": "boolean", "description": "If true (default), download and send the full song too."},
            }, "required": ["url"]},
        }},
        find_song_of_video,
    )
