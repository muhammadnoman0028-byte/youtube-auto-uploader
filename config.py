"""Settings, paths and small shared helpers for the YouTube auto-uploader."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "data"
STATE_PATH = DATA_DIR / "state.json"
LOG_PATH = DATA_DIR / "uploader.log"
THUMB_CACHE_DIR = DATA_DIR / "thumbs"
UPLOAD_LOCK_PATH = DATA_DIR / "upload.lock"

DEFAULTS: Dict[str, Any] = {
    # Where the channel downloader stores one sub-folder per video.
    "library_dir": "D:/Projects/YouTube_Downloads",
    # "local" = this machine's timezone, or an IANA name such as "Europe/Paris".
    "timezone": "local",
    # Every video is scheduled inside this daily window.
    "publish_window_start": "20:00",
    "publish_window_end": "21:00",
    # The uploader wakes up this many minutes before the publish time.
    "upload_lead_minutes": 90,
    # Hard cap for automatic uploads. 1 = one video per day.
    "max_uploads_per_day": 1,
    # 0 = Monday ... 6 = Sunday. Days listed here are skipped when planning.
    "skip_weekdays": [],
    "category_id": "22",
    "default_language": "fr",
    "playlist_id": "",
    "made_for_kids": False,
    "extra_tags": [],
    "max_tags": 15,
    "title_prefix": "",
    "title_suffix": "",
    "description_footer": "",
    # reschedule = move a missed slot to the next free day
    # publish_now = upload it publicly as soon as the uploader runs
    "missed_policy": "reschedule",
    # After each upload, poll YouTube for a copyright rejection:
    # "draft" = clear the publish time and keep it private; "delete" = remove it.
    "copyright_guard": True,
    "copyright_policy": "draft",
    "copyright_check_seconds": 120,
    "notify_subscribers": True,
    "oauth_client_secret": "client_secret.json",
    "oauth_token": "data/token.json",
}

_lock = threading.RLock()


def atomic_write(path: Path, text: str) -> None:
    """Write text to *path* atomically (Windows-safe with small retries)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    last: Exception | None = None
    for _ in range(8):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:  # target briefly locked by a reader
            last = exc
            time.sleep(0.15)
    if last:
        raise last


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _as_tags(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, (list, tuple)):
        return []
    tags: list[str] = []
    for raw in value:
        tag = re.sub(r"\s+", " ", str(raw)).strip()
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def _as_weekdays(value: Any) -> list[int]:
    if isinstance(value, str):
        value = [part for part in re.split(r"[,\s]+", value) if part]
    if not isinstance(value, (list, tuple)):
        return []
    days: list[int] = []
    for raw in value:
        try:
            day = int(raw)
        except (TypeError, ValueError):
            continue
        if 0 <= day <= 6 and day not in days:
            days.append(day)
    return sorted(days)


def _valid_hhmm(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not match:
        return fallback
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return fallback
    return f"{hour:02d}:{minute:02d}"


def normalise(cfg: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(DEFAULTS)
    out.update({k: v for k, v in cfg.items() if k in DEFAULTS and v is not None})

    out["library_dir"] = str(out["library_dir"]).strip() or DEFAULTS["library_dir"]
    out["timezone"] = str(out["timezone"]).strip() or "local"
    out["publish_window_start"] = _valid_hhmm(
        out.get("publish_window_start"), DEFAULTS["publish_window_start"]
    )
    out["publish_window_end"] = _valid_hhmm(
        out.get("publish_window_end"), DEFAULTS["publish_window_end"]
    )
    start_min = _minutes(out["publish_window_start"])
    end_min = _minutes(out["publish_window_end"])
    if end_min <= start_min:
        out["publish_window_start"] = DEFAULTS["publish_window_start"]
        out["publish_window_end"] = DEFAULTS["publish_window_end"]

    out["upload_lead_minutes"] = _as_int(out.get("upload_lead_minutes"), 90, 5, 720)
    out["max_uploads_per_day"] = _as_int(out.get("max_uploads_per_day"), 1, 1, 6)
    out["max_tags"] = _as_int(out.get("max_tags"), 15, 1, 60)
    out["skip_weekdays"] = _as_weekdays(out.get("skip_weekdays"))
    out["extra_tags"] = _as_tags(out.get("extra_tags"))
    out["category_id"] = str(out.get("category_id") or "22").strip() or "22"
    out["default_language"] = str(out.get("default_language") or "fr").strip() or "fr"
    out["playlist_id"] = str(out.get("playlist_id") or "").strip()
    out["title_prefix"] = str(out.get("title_prefix") or "")
    out["title_suffix"] = str(out.get("title_suffix") or "")
    out["description_footer"] = str(out.get("description_footer") or "")
    out["made_for_kids"] = bool(out.get("made_for_kids"))
    out["notify_subscribers"] = bool(out.get("notify_subscribers"))
    out["missed_policy"] = (
        out.get("missed_policy") if out.get("missed_policy") in ("reschedule", "publish_now") else "reschedule"
    )
    out["copyright_guard"] = bool(out.get("copyright_guard"))
    out["copyright_policy"] = (
        out.get("copyright_policy") if out.get("copyright_policy") in ("draft", "delete") else "draft"
    )
    out["copyright_check_seconds"] = _as_int(
        out.get("copyright_check_seconds"), 120, 0, 900
    )
    out["oauth_client_secret"] = str(out.get("oauth_client_secret") or "client_secret.json")
    out["oauth_token"] = str(out.get("oauth_token") or "data/token.json")
    return out


def _minutes(hhmm: str) -> int:
    hour, minute = hhmm.split(":")
    return int(hour) * 60 + int(minute)


def load_config() -> Dict[str, Any]:
    with _lock:
        cfg = dict(DEFAULTS)
        if CONFIG_PATH.is_file():
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
                if isinstance(raw, dict):
                    cfg.update({k: v for k, v in raw.items() if k in DEFAULTS})
            except Exception as exc:  # keep running with defaults
                logging.getLogger("config").warning("Could not read %s: %s", CONFIG_PATH, exc)
        return normalise(cfg)


def save_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    with _lock:
        clean = normalise(cfg)
        atomic_write(CONFIG_PATH, json.dumps(clean, ensure_ascii=False, indent=2) + "\n")
        return clean


def public_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Config fields the dashboard needs (paths included for transparency)."""
    hidden = {"oauth_token"}
    return {key: value for key, value in cfg.items() if key not in hidden}


def setup_logging(verbose: bool = False) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if getattr(root, "_yt_uploader_configured", False):
        return
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    root._yt_uploader_configured = True  # type: ignore[attr-defined]
