"""Vision extension: let the agent SEE photos and video frames.

Mercury 3 is text-only, so this uses Mercury Decide (mercury-decide) on the
same Inception API (POST /v1/decisions) with the image in the shared state.
Decide answers typed questions (yes/no probability, choice, score) — not
free text — so an open "what is in this photo?" runs a small battery of
questions and the answers are composed into a Persian description. Local
image facts (size, format, dominant colors, brightness) come from Pillow
and are always available, even if the Decide call fails.

Image format follows the Inception docs: chat-style parts such as
{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}
in `state`; base64 data URLs only, at most 10 MiB per image.

Schemas follow the OpenAI function-calling format exactly:
{"type": "function", "function": {"name", "description", "parameters"}}.
Never put the handler function (or any Python object) inside the schema.
"""

import asyncio
import base64
import io
import json
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import httpx

_REGISTRY = None
_MAX_IMAGE_BYTES = 10 * 1024 * 1024

_SUBJECT_LABELS = {
    "person": "آدم",
    "animal": "حیوان",
    "food": "غذا یا نوشیدنی",
    "vehicle": "ماشین یا وسیله نقلیه",
    "building": "ساختمان یا نمای شهر",
    "nature": "طبیعت یا منظره",
    "object": "یک شیء",
    "other": "چیز دیگر",
}

_KIND_LABELS = {
    "photo": "عکس واقعی",
    "screenshot_web": "اسکرین‌شات از صفحه وب یا جستجوی اینترنتی",
    "screenshot_chat": "اسکرین‌شات از چت یا پیام‌رسان",
    "document": "سند یا صفحه متن",
    "other": "چیز دیگر",
}


# ---------------------------------------------------------------- helpers

def _media_dir() -> Path:
    try:
        from config import settings
        d = Path(settings.TEMP_MEDIA_DIR)
    except Exception:
        d = Path("data/temp_media")
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _load_image_bytes(source: str) -> Tuple[Optional[bytes], str]:
    """Load image bytes from a local path or an http(s) URL. Returns (bytes, mime)."""
    if source.startswith("http://") or source.startswith("https://"):
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(source, follow_redirects=True)
            if resp.status_code != 200 or len(resp.content) > _MAX_IMAGE_BYTES:
                return None, ""
            mime = (resp.headers.get("content-type") or "").split(";")[0].strip()
            return resp.content, mime
        except Exception:
            return None, ""
    p = Path(source)
    if not p.exists() or not p.is_file() or p.stat().st_size > _MAX_IMAGE_BYTES:
        return None, ""
    return p.read_bytes(), ""


def _local_facts(data: bytes) -> Dict[str, Any]:
    """Guaranteed local image facts via Pillow (no model needed)."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        img.load()
        w, h = img.size
        fmt = img.format or "?"
        rgb = img.convert("RGB")
        avg = rgb.resize((1, 1)).getpixel((0, 0))
        avg_hex = "#%02x%02x%02x" % avg
        small = rgb.copy()
        small.thumbnail((64, 64))
        q = small.quantize(colors=5)
        palette = q.getpalette() or []
        colors = []
        for count, idx in sorted(q.getcolors() or [], reverse=True)[:3]:
            r, g, b = palette[idx * 3:idx * 3 + 3]
            colors.append("#%02x%02x%02x" % (r, g, b))
        gray = img.convert("L")
        px = list(gray.getdata())
        brightness = round(sum(px) / max(1, len(px)))
        return {"format": fmt, "width": w, "height": h,
                "average_color": avg_hex, "top_colors": colors,
                "brightness_0_255": brightness}
    except Exception as exc:
        return {"error": f"خواندن عکس ناموفق بود: {exc}"}


def _to_data_url(data: bytes) -> Optional[str]:
    """Downscale + JPEG-encode for the Decide call. Returns a data URL or None."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data)).convert("RGB")
        img.thumbnail((1024, 1024))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"
    except Exception:
        return None


async def _ai_config() -> Dict[str, Any]:
    """Read the agent's own AI config (same key/base URL as Mercury 3)."""
    try:
        from ai.model_manager import ModelManager
        mm = ModelManager(_REGISTRY.lt_memory)
        await mm.load_from_memory()
        return mm.get_config()
    except Exception:
        return {}


def _build_questions(question: str) -> Dict[str, Any]:
    """One yes/no question for a direct question, else a descriptive battery."""
    q = question.strip()
    lower = q.lower()
    if q.startswith("آیا") or lower.startswith(("is there", "is this", "does", "are there", "do you see")):
        return {"answer": {"type": "noul", "instructions": q}}
    return {
        "people": {"type": "noul", "instructions": "Is there at least one person visible in the image?"},
        "animal": {"type": "noul", "instructions": "Is there an animal visible in the image?"},
        "text": {"type": "noul", "instructions": "Is there readable text in the image?"},
        "outdoor": {"type": "noul", "instructions": "Is this photo taken outdoors?"},
        "subject": {"type": "choice",
                    "instructions": "What is the main subject of this image?",
                    "criteria": {
                        "person": "A person or people",
                        "animal": "An animal",
                        "food": "Food or a drink",
                        "vehicle": "A car or other vehicle",
                        "building": "A building or city view",
                        "nature": "Nature or landscape",
                        "object": "An everyday object",
                        "other": "Something else",
                    }},
        "kind": {"type": "choice",
                 "instructions": "What kind of image is this?",
                 "criteria": {
                     "photo": "A real photo taken with a camera",
                     "screenshot_web": "A screenshot of a web page or a web search",
                     "screenshot_chat": "A screenshot of a chat or messaging app",
                     "document": "A document or a page of text",
                     "other": "Something else",
                 }},
    }


def _compose_persian(answers: Dict[str, Any], question: str) -> str:
    """Turn Decide answers into a short Persian description."""
    if "answer" in answers:
        p = float((answers.get("answer") or {}).get("noul", 0) or 0)
        if p >= 0.7:
            return f"بله، به احتمال زیاد درست است. ({question})"
        if p >= 0.4:
            return f"مشخص نیست؛ احتمالش حدود {round(p * 100)}٪ است. ({question})"
        return f"نه، به احتمال زیاد درست نیست. ({question})"
    parts = []
    def _noul(key: str, yes_text: str) -> None:
        a = answers.get(key) or {}
        p = float(a.get("noul", 0) or 0)
        if p >= 0.6:
            parts.append(yes_text)
    _noul("people", "یک یا چند نفر دیده می‌شود.")
    _noul("animal", "یک حیوان دیده می‌شود.")
    _noul("text", "متن خوانا دارد.")
    _noul("outdoor", "فضای باز است.")
    subj = (answers.get("subject") or {}).get("choice")
    if subj:
        parts.append(f"موضوع اصلی عکس: {_SUBJECT_LABELS.get(subj, subj)}.")
    kind = (answers.get("kind") or {}).get("choice")
    if kind:
        parts.append(f"نوع عکس: {_KIND_LABELS.get(kind, kind)}.")
    if not parts:
        return "چیز مشخصی از عکس نفهمیدم."
    return " ".join(parts)


# ---------------------------------------------------------------- tools

async def describe_image(args: Dict[str, Any]) -> Dict[str, Any]:
    source = (args.get("image_path") or "").strip()
    question = (args.get("question") or "").strip()
    if not source:
        return {"status": "error", "error": "مسیر عکس یا لینک آن را نفرستادی."}
    data, _mime = await _load_image_bytes(source)
    if data is None:
        return {"status": "error", "error": "عکس پیدا نشد یا دانلود نشد؛ مسیر درست یا لینک مستقیم عکس را بده."}
    facts = _local_facts(data)
    facts_text = ""
    if "width" in facts:
        facts_text = (f"عکس {facts['width']} در {facts['height']} پیکسل، فرمت {facts['format']}، "
                      f"رنگ غالب {facts.get('average_color', '?')}.")
    data_url = _to_data_url(data)
    if data_url is None:
        return {"status": "ok", "description": facts_text or "عکس خوانده شد ولی تحلیل نشد.",
                "facts": facts, "message": facts_text}
    cfg = await _ai_config()
    base_url = (cfg.get("base_url") or "").rstrip("/")
    api_key = cfg.get("api_key") or ""
    if not base_url or not api_key:
        return {"status": "ok", "description": facts_text + " (مدل بینایی در دسترس نیست؛ تنظیمات هوش مصنوعی را بررسی کن.)",
                "facts": facts, "message": facts_text}
    # The agent's base_url already ends in /v1 (https://api.inceptionlabs.ai/v1),
    # so the Decide endpoint is {base_url}/decisions — NOT {base_url}/v1/decisions
    # (that double /v1/v1 returned HTTP 404).
    decide_url = f"{base_url}/decisions" if base_url.endswith("/v1") else f"{base_url}/v1/decisions"
    payload = {
        "model": "mercury-decide",
        "state": [
            {"type": "text", "text": "Look at this image carefully and answer the questions about it."},
            {"type": "image_url", "image_url": {"url": data_url}},
        ],
        "questions": _build_questions(question),
    }
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                decide_url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
    except Exception as exc:
        return {"status": "ok",
                "description": facts_text + f" (مدل بینایی جواب نداد: {exc})",
                "facts": facts, "message": facts_text}
    if resp.status_code != 200:
        return {"status": "ok",
                "description": facts_text + f" (مدل بینایی خطا داد: HTTP {resp.status_code} {resp.text[:150]})",
                "facts": facts, "message": facts_text}
    try:
        answers = resp.json().get("answers", {}) or {}
    except Exception:
        return {"status": "ok", "description": facts_text, "facts": facts, "message": facts_text}
    desc = _compose_persian(answers, question)
    full = f"{desc} {facts_text}".strip()
    return {"status": "ok", "description": full, "facts": facts, "message": full}


async def describe_video(args: Dict[str, Any]) -> Dict[str, Any]:
    path_str = (args.get("video_path") or "").strip()
    question = (args.get("question") or "").strip()
    if not path_str or not Path(path_str).exists():
        return {"status": "error", "error": "فایل ویدئو پیدا نشد؛ مسیر درست فایل را بده."}
    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return {"status": "error", "error": "ffmpeg در دسترس نیست؛ نمی‌توانم از ویدئو فریم بگیرم."}
    out = _media_dir() / f"frame_{uuid.uuid4().hex[:10]}.jpg"
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            [ffmpeg, "-y", "-ss", "3", "-i", str(path_str), "-frames:v", "1", "-q:v", "3", str(out)],
            capture_output=True, timeout=120,
        )
    except Exception as exc:
        return {"status": "error", "error": f"گرفتن فریم از ویدئو ناموفق بود: {exc}"}
    if not out.exists() or out.stat().st_size == 0:
        return {"status": "error", "error": "نتوانستم از ویدئو فریم بگیرم."}
    return await describe_image({"image_path": str(out), "question": question})


# ---------------------------------------------------------------- registration

def register(registry):
    global _REGISTRY
    _REGISTRY = registry
    registry.register_extension_tool(
        "describe_image",
        {"type": "function", "function": {
            "name": "describe_image",
            "description": "Look at a photo (local file path or direct image URL) and describe what is in it, or answer a question about it. Mercury 3 cannot see images, so this uses Mercury Decide vision on the same API. It describes the scene and objects, but it cannot read small text inside screenshots — do not ask it to transcribe text.",
            "parameters": {"type": "object", "properties": {
                "image_path": {"type": "string", "description": "Local file path of the image, or a direct http(s) image URL."},
                "question": {"type": "string", "description": "Optional question about the image (for example: is there a cat in this photo?). Without it, the image is described in general."},
            }, "required": ["image_path"]},
        }},
        describe_image,
    )
    registry.register_extension_tool(
        "describe_video",
        {"type": "function", "function": {
            "name": "describe_video",
            "description": "Look at a video file: takes a frame from it and describes what is in the frame, or answers a question about it.",
            "parameters": {"type": "object", "properties": {
                "video_path": {"type": "string", "description": "Local file path of the video."},
                "question": {"type": "string", "description": "Optional question about the video frame."},
            }, "required": ["video_path"]},
        }},
        describe_video,
    )
