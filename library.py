"""Turn each downloader sub-folder into an upload-ready library item.

The YouTube channel downloader writes:

    <root>/<Video Title>/
        <Video Title>.mp4
        <Video Title>.jpg            (original YouTube thumbnail)
        <Video Title>.info.json      (raw metadata)
        <Video Title>.description
        thumbnail.jpg                (redesigned thumbnail, 1280x720)

This module finds those files, applies the configured title/tag/footer
decorations, and reports anything YouTube would reject.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageOps

import config as appconfig

logger = logging.getLogger("library")

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
FRAGMENT_RE = re.compile(r"\.f\d+(?:-\d+)?\.[^.]+$", re.IGNORECASE)
# Trailing "| Boris Cyrulnik" (and variants) is dropped only when a title is too long.
BORIS_RE = re.compile(
    r"\s*(?:[|｜\-–—_:·•]\s*)?Boris\s+Cyrulnik(?:\s+(?:explique|explains))?\s*$",
    re.IGNORECASE,
)
TITLE_MAX = 100
DESCRIPTION_MAX = 5000
# A video whose audio track runs much longer than the picture is truncated and
# YouTube will fail to process it, so it must not be uploaded.
AV_GAP_TOLERANCE = 10.0
TAG_BUDGET = 480

CATEGORY_MAP = {
    "film & animation": "1",
    "autos & vehicles": "2",
    "music": "10",
    "pets & animals": "15",
    "sports": "17",
    "short movies": "18",
    "travel & events": "19",
    "gaming": "20",
    "videoblogging": "21",
    "people & blogs": "22",
    "comedy": "23",
    "entertainment": "24",
    "news & politics": "25",
    "howto & style": "26",
    "education": "27",
    "science & technology": "28",
    "nonprofits & activism": "29",
}


def human_size(num: int | float | None) -> str:
    if not num:
        return "—"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def format_duration(seconds: Any) -> str:
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "—"
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def media_gap(path: Path | str) -> Optional[float]:
    """Seconds between the video and audio track lengths, or None if unknown."""
    try:
        import av

        with av.open(str(path)) as container:
            video = container.streams.video[0] if container.streams.video else None
            audio = container.streams.audio[0] if container.streams.audio else None
            if not video or not audio or not video.duration or not audio.duration:
                return None
            video_len = float(video.duration * video.time_base)
            audio_len = float(audio.duration * audio.time_base)
            return abs(video_len - audio_len)
    except Exception as exc:
        logger.debug("Could not read stream lengths for %s: %s", path, exc)
        return None


def _clean_title(raw: Any) -> Tuple[str, List[str]]:
    warnings: List[str] = []
    title = re.sub(r"\s+", " ", str(raw or "")).strip()
    if "<" in title or ">" in title:
        title = title.replace("<", "").replace(">", "").strip()
        warnings.append("Title contained < or > (removed for YouTube).")
    if len(title) > TITLE_MAX:
        # Keep the real title intact: first try dropping the trailing author name.
        shortened = BORIS_RE.sub("", title).strip(" \t|｜-–—_:·•")
        if shortened and shortened != title:
            title = shortened
            warnings.append(
                "Removed trailing 'Boris Cyrulnik' so the title fits in "
                f"{TITLE_MAX} characters."
            )
    if len(title) > TITLE_MAX:
        cut = title[: TITLE_MAX - 3].rstrip()
        if " " in cut:
            cut = cut[: cut.rfind(" ")].rstrip()
        title = cut.rstrip(" -–—|_") + "..."
        warnings.append(f"Title truncated to {TITLE_MAX} characters.")
    return title, warnings


def fit_title(raw: Any) -> str:
    """Normalise a title for YouTube (100 characters, drops the author suffix)."""
    title, _ = _clean_title(raw)
    return title


def _clean_description(raw: Any, footer: str = "") -> Tuple[str, List[str]]:
    warnings: List[str] = []
    description = str(raw or "").replace("\r\n", "\n").strip()
    if footer.strip():
        description = (description + "\n\n" + footer.strip()).strip() if description else footer.strip()
    if len(description) > DESCRIPTION_MAX:
        description = description[: DESCRIPTION_MAX - 1].rstrip() + "…"
        warnings.append(f"Description truncated to {DESCRIPTION_MAX} characters.")
    if not description:
        warnings.append("Description is empty.")
    return description, warnings


def _clean_tags(raw_tags: Any, cfg: Dict[str, Any]) -> List[str]:
    candidates: List[Any] = []
    if isinstance(raw_tags, (list, tuple)):
        candidates.extend(raw_tags)
    candidates.extend(cfg.get("extra_tags") or [])
    limit = int(cfg.get("max_tags") or 15)

    tags: List[str] = []
    seen: set[str] = set()
    for raw in candidates:
        tag = re.sub(r"\s+", " ", str(raw or "")).strip()
        tag = tag.replace("<", "").replace(">", "")
        if not tag or tag.lower() in seen:
            continue
        if len(tag) > 60:
            tag = tag[:60].rstrip()
        probe = ",".join(tags + [tag])
        if len(tags) >= limit or len(probe) > TAG_BUDGET:
            break
        tags.append(tag)
        seen.add(tag.lower())
    return tags


def _pick_category(info: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    for name in info.get("categories") or []:
        mapped = CATEGORY_MAP.get(str(name).strip().lower())
        if mapped:
            return mapped
    fallback = str(cfg.get("category_id") or "22")
    return fallback if fallback.isdigit() else "22"


def _pick_language(info: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    default = str(cfg.get("default_language") or "fr")
    lang = str(info.get("language") or default).strip()
    if re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", lang):
        return lang
    return default


def _find_video(folder: Path) -> Optional[Path]:
    files = [
        path
        for path in folder.iterdir()
        if path.is_file()
        and path.suffix.lower() in VIDEO_EXTS
        and not path.name.endswith(".part")
    ]
    final_files = [path for path in files if not FRAGMENT_RE.search(path.name)]
    if not final_files:
        return None
    # Prefer the merged .mp4, then the biggest file.
    return max(final_files, key=lambda path: (path.suffix.lower() == ".mp4", path.stat().st_size))


def _find_thumbnail(folder: Path, video: Path) -> Optional[Path]:
    preferred = [
        folder / "thumbnail.jpg",
        folder / "thumbnail.jpeg",
        folder / "thumbnail.png",
    ]
    for path in preferred:
        if path.is_file() and path.stat().st_size > 0:
            return path
    stem = video.stem
    for ext in IMAGE_EXTS:
        path = folder / f"{stem}{ext}"
        if path.is_file() and path.stat().st_size > 0:
            return path
    for ext in IMAGE_EXTS:
        for path in sorted(folder.glob(f"*{ext}")):
            if path.is_file() and path.stat().st_size > 0:
                return path
    return None


def _find_info(folder: Path, video: Path) -> Optional[Path]:
    exact = folder / f"{video.stem}.info.json"
    if exact.is_file():
        return exact
    matches = sorted(folder.glob("*.info.json"))
    return matches[0] if matches else None


def _find_description(folder: Path, video: Path) -> Optional[Path]:
    exact = folder / f"{video.stem}.description"
    if exact.is_file() and exact.stat().st_size > 0:
        return exact
    matches = sorted(folder.glob("*.description"))
    return matches[0] if matches else None


def _read_json(path: Optional[Path]) -> Dict[str, Any]:
    if not path or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Could not parse %s: %s", path, exc)
        return {}


def _read_text(path: Optional[Path]) -> str:
    if not path or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return ""


def _source_date(info: Dict[str, Any]) -> Optional[str]:
    raw = str(info.get("upload_date") or "")
    if re.fullmatch(r"\d{8}", raw):
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    timestamp = info.get("timestamp")
    if isinstance(timestamp, (int, float)) and timestamp > 0:
        return datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()
    return None


def _thumbnail_warnings(path: Optional[Path]) -> List[str]:
    if not path:
        return ["No thumbnail image found (YouTube will pick a frame)."]
    warnings: List[str] = []
    try:
        size = path.stat().st_size
        if size > 2 * 1024 * 1024:
            warnings.append("Thumbnail is over 2 MB; it will be re-encoded before upload.")
        with Image.open(path) as img:
            width, height = img.size
            if width < 640 or height < 360:
                warnings.append(f"Thumbnail is only {width}x{height}; it will be upscaled.")
            if abs((width / height) - (16 / 9)) > 0.05:
                warnings.append(f"Thumbnail aspect ratio is {width}x{height} (16:9 recommended).")
    except Exception as exc:
        warnings.append(f"Thumbnail could not be read: {exc}")
    return warnings


def _scan_folder(folder: Path, cfg: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    video = _find_video(folder)
    if not video:
        return None, "no finished video file"

    info_path = _find_info(folder, video)
    info = _read_json(info_path)
    description_path = _find_description(folder, video)
    thumbnail_path = _find_thumbnail(folder, video)

    base_title = info.get("title") or folder.name
    title, warnings = _clean_title(f"{cfg.get('title_prefix', '')}{base_title}{cfg.get('title_suffix', '')}")
    raw_description = _read_text(description_path) or str(info.get("description") or "")
    description, desc_warnings = _clean_description(raw_description, str(cfg.get("description_footer") or ""))
    warnings.extend(desc_warnings)
    warnings.extend(_thumbnail_warnings(thumbnail_path))

    try:
        video_size = video.stat().st_size
    except OSError:
        video_size = 0
    if video_size > 128 * 1024 * 1024 * 1024:
        warnings.append("Video is larger than YouTube's 128 GB limit.")

    gap = media_gap(video)
    av_mismatch = gap is not None and gap > AV_GAP_TOLERANCE
    if av_mismatch:
        warnings.append(
            "Truncated download: the audio track is "
            f"{gap:.0f} s longer than the video track, so YouTube would fail "
            "to process it. Re-download this video before uploading."
        )

    category_id = _pick_category(info, cfg)
    language = _pick_language(info, cfg)
    duration = info.get("duration")

    item: Dict[str, Any] = {
        "folder": folder.name,
        "folder_path": str(folder),
        "video_path": str(video),
        "video_name": video.name,
        "video_size": video_size,
        "thumbnail_path": str(thumbnail_path) if thumbnail_path else None,
        "description_path": str(description_path) if description_path else None,
        "info_path": str(info_path) if info_path else None,
        "title": title,
        "description": description,
        "tags": _clean_tags(info.get("tags"), cfg),
        "category_id": category_id,
        "language": language,
        "duration": duration,
        "source_id": info.get("id"),
        "source_url": info.get("webpage_url"),
        "source_date": _source_date(info),
        "age_limit": info.get("age_limit") or 0,
        "warnings": warnings,
        "av_mismatch": av_mismatch,
        "av_gap": round(gap, 1) if gap is not None else None,
        "description_length": len(description),
        "video_size_human": human_size(video_size),
        "duration_human": format_duration(duration),
    }
    return item, ""


def scan_library(root: Path | str, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Scan every immediate sub-folder of *root* that contains a finished video."""
    root = Path(root).expanduser()
    if not root.is_dir():
        raise NotADirectoryError(f"Library folder not found: {root}")

    items: List[Dict[str, Any]] = []
    for folder in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name.lower()):
        if folder.name.startswith("."):
            continue
        try:
            item, reason = _scan_folder(folder, cfg)
        except Exception as exc:
            logger.exception("Failed to scan %s", folder)
            item, reason = None, f"scan error: {exc}"
        if item:
            items.append(item)
        elif reason:
            logger.debug("Skipped %s: %s", folder.name, reason)
    return items


def prepare_thumbnail(src: Path | str, cache_dir: Path | str | None = None) -> Tuple[Path, List[str]]:
    """Return a YouTube-safe JPEG/PNG (≤2 MB, ≥640 px wide) for upload."""
    src = Path(src)
    cache_dir = Path(cache_dir or appconfig.THUMB_CACHE_DIR)
    warnings: List[str] = []

    if not src.is_file():
        raise FileNotFoundError(f"Thumbnail not found: {src}")

    suffix = src.suffix.lower()
    size = src.stat().st_size
    if suffix in (".jpg", ".jpeg", ".png") and size <= 2 * 1024 * 1024:
        try:
            with Image.open(src) as img:
                if img.width >= 640 and img.height >= 360:
                    return src, warnings
        except Exception:
            pass

    cache_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{src.resolve()}|{int(src.stat().st_mtime)}|{size}"
    name = hashlib.sha1(stamp.encode("utf-8")).hexdigest()[:16] + ".jpg"
    out = cache_dir / name
    if out.is_file() and out.stat().st_size <= 2 * 1024 * 1024:
        return out, ["Thumbnail re-encoded for YouTube."]

    with Image.open(src) as img:
        img = img.convert("RGB")
        if abs((img.width / img.height) - (16 / 9)) > 0.03 or img.width < 1280:
            img = ImageOps.fit(img, (1280, 720), Image.LANCZOS, centering=(0.5, 0.5))
        quality = 92
        while quality >= 70:
            img.save(out, format="JPEG", quality=quality, optimize=True)
            if out.stat().st_size <= 2 * 1024 * 1024:
                break
            quality -= 8
    warnings.append("Thumbnail re-encoded for YouTube.")
    return out, warnings
