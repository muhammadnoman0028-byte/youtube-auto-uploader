"""Bulk schedule + one-upload-per-day engine.

The scheduler keeps a JSON plan in ``data/state.json``:

    every downloader sub-folder  ->  one local calendar day
    publish time                 ->  random minute inside 20:00-21:00
    upload time                  ->  publish time minus 90 minutes

``plan()`` assigns all pending videos in bulk. ``upload_next()`` uploads the
next due video as a private YouTube video with ``publishAt`` set, so YouTube
makes it public automatically at the planned time.
"""

from __future__ import annotations

import json
import logging
import random
import threading
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import config as appconfig
import library
import yt_client

logger = logging.getLogger("scheduler")

EventCallback = Callable[[str, Dict[str, Any]], None]
ProgressCallback = Callable[[float, int, int], None]

ACTIVE_STATUSES = {"pending", "failed", "uploading"}
MAX_AUTO_ATTEMPTS = 5
RETRY_DELAYS_MIN = [15, 60, 240, 720, 1440]


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def get_timezone(cfg: Dict[str, Any]):
    name = str(cfg.get("timezone") or "local").strip()
    if name.lower() in ("", "local", "system"):
        try:
            from tzlocal import get_localzone

            return get_localzone()
        except Exception:
            return datetime.now().astimezone().tzinfo
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception as exc:
        logger.warning("Unknown timezone %r (%s); using the system timezone", name, exc)
        return datetime.now().astimezone().tzinfo


def window_minutes(cfg: Dict[str, Any]) -> Tuple[int, int]:
    def parse(hhmm: str) -> int:
        hour, minute = str(hhmm).split(":")
        return int(hour) * 60 + int(minute)

    start = parse(cfg.get("publish_window_start") or "20:00")
    end = parse(cfg.get("publish_window_end") or "21:00")
    if end <= start:
        start, end = 20 * 60, 21 * 60
    return start, end


class UploadBusy(RuntimeError):
    pass


class _ProcessUploadLock:
    """A coarse cross-process lock so a Task Scheduler run and the dashboard
    daemon never upload two videos at the same time."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._handle = open(self.path, "a+b")
            self._handle.seek(0)
            try:
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            except ImportError:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if self._handle:
                self._handle.close()
                self._handle = None
            return False

    def release(self) -> None:
        if not self._handle:
            return
        try:
            self._handle.seek(0)
            try:
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            except ImportError:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._handle.close()
        finally:
            self._handle = None


# --------------------------------------------------------------------------- #
# Scheduler                                                                    #
# --------------------------------------------------------------------------- #
class Scheduler:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._upload_lock = threading.Lock()
        self._process_lock = _ProcessUploadLock(appconfig.UPLOAD_LOCK_PATH)
        self.config = appconfig.load_config()
        self._config_mtime = self._mtime(appconfig.CONFIG_PATH)
        self.state = self._read_state()
        self._state_mtime = self._mtime(appconfig.STATE_PATH)

    # -- persistence ------------------------------------------------------- #
    @staticmethod
    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    def _read_state(self) -> Dict[str, Any]:
        if appconfig.STATE_PATH.is_file():
            try:
                data = json.loads(appconfig.STATE_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("videos"), list):
                    data.setdefault("version", 1)
                    data.setdefault("updated_at", None)
                    data.setdefault("last_upload_date", None)
                    return data
            except Exception as exc:
                logger.error("Could not read %s: %s", appconfig.STATE_PATH, exc)
        return {"version": 1, "updated_at": None, "last_upload_date": None, "videos": []}

    def _refresh(self) -> None:
        config_mtime = self._mtime(appconfig.CONFIG_PATH)
        if config_mtime != self._config_mtime:
            self.config = appconfig.load_config()
            self._config_mtime = config_mtime
        state_mtime = self._mtime(appconfig.STATE_PATH)
        if state_mtime and state_mtime != self._state_mtime:
            self.state = self._read_state()
            self._state_mtime = state_mtime

    def _save(self) -> None:
        self.state["updated_at"] = utc_iso(utc_now())
        appconfig.atomic_write(
            appconfig.STATE_PATH,
            json.dumps(self.state, ensure_ascii=False, indent=2) + "\n",
        )
        self._state_mtime = self._mtime(appconfig.STATE_PATH)

    # -- lookup / summary -------------------------------------------------- #
    def find(self, folder: str) -> Optional[Dict[str, Any]]:
        target = str(folder or "").strip().lower()
        if not target:
            return None
        for video in self.state["videos"]:
            if str(video.get("folder", "")).lower() == target:
                return video
        # Convenience for the CLI: allow a unique partial folder name.
        matches = [
            video
            for video in self.state["videos"]
            if target in str(video.get("folder", "")).lower()
        ]
        return matches[0] if len(matches) == 1 else None

    @property
    def busy(self) -> bool:
        return self._upload_lock.locked()

    def _now(self) -> Tuple[datetime, datetime]:
        tz = get_timezone(self.config)
        local = datetime.now(tz)
        return local, local.astimezone(timezone.utc)

    def _uploads_today(self) -> int:
        today = self._now()[0].date().isoformat()
        return sum(
            1
            for video in self.state["videos"]
            if video.get("status") == "uploaded" and video.get("uploaded_local_date") == today
        )

    def summary(self) -> Dict[str, Any]:
        with self._lock:
            self._refresh()
            videos = self.state["videos"]
            counts = {
                status: sum(1 for video in videos if video.get("status") == status)
                for status in ("pending", "uploaded", "failed", "uploading", "skipped", "missing")
            }
            scheduled = [
                video
                for video in videos
                if video.get("status") in ("pending", "failed", "uploading") and video.get("publish_at")
            ]
            unscheduled = [
                video
                for video in videos
                if video.get("status") in ("pending", "failed") and not video.get("publish_at")
            ]
            next_video = min(scheduled, key=lambda video: video["publish_at"]) if scheduled else None
            lead = timedelta(minutes=int(self.config.get("upload_lead_minutes") or 90))
            next_upload_at = None
            if next_video:
                publish = parse_iso(next_video.get("publish_at"))
                if publish:
                    next_upload_at = utc_iso(publish - lead)
            return {
                "total": len(videos),
                "scheduled": len(scheduled),
                "unscheduled": len(unscheduled),
                "uploaded": counts["uploaded"],
                "failed": counts["failed"],
                "skipped": counts["skipped"],
                "missing": counts["missing"],
                "uploading": counts["uploading"],
                "uploads_today": self._uploads_today(),
                "max_uploads_per_day": int(self.config.get("max_uploads_per_day") or 1),
                "next": self._ui_video(next_video) if next_video else None,
                "next_upload_at": next_upload_at,
                "library_dir": self.config.get("library_dir"),
                "window": [
                    self.config.get("publish_window_start"),
                    self.config.get("publish_window_end"),
                ],
            }

    def _ui_video(self, video: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not video:
            return None
        description = str(video.get("description") or "")
        return {
            "folder": video.get("folder"),
            "title": video.get("title"),
            "status": video.get("status"),
            "slot_date": video.get("slot_date"),
            "publish_at": video.get("publish_at"),
            "publish_time_local": video.get("publish_time_local"),
            "uploaded_at": video.get("uploaded_at"),
            "youtube_id": video.get("youtube_id"),
            "youtube_url": video.get("youtube_url"),
            "attempts": video.get("attempts") or 0,
            "last_error": video.get("last_error"),
            "note": video.get("note"),
            "duration": video.get("duration"),
            "duration_human": video.get("duration_human") or library.format_duration(video.get("duration")),
            "video_size": video.get("video_size"),
            "video_size_human": video.get("video_size_human") or library.human_size(video.get("video_size")),
            "source_date": video.get("source_date"),
            "source_id": video.get("source_id"),
            "source_url": video.get("source_url"),
            "description_length": len(description),
            "description_preview": description[:240],
            "tags": video.get("tags") or [],
            "has_thumbnail": bool(video.get("thumbnail_path")),
            "warnings": video.get("warnings") or [],
            "thumbnail_url": (
                "/api/v1/thumbnail?folder=" + str(video.get("folder"))
                if video.get("thumbnail_path")
                else None
            ),
        }

    def ui_videos(self) -> List[Dict[str, Any]]:
        with self._lock:
            self._refresh()
            videos = [self._ui_video(video) for video in self.state["videos"]]
        videos = [video for video in videos if video]
        videos.sort(
            key=lambda video: (
                video.get("publish_at") or "9999-99-99T99:99:99Z",
                (video.get("folder") or "").lower(),
            )
        )
        return videos

    # -- scanning ---------------------------------------------------------- #
    def scan(self) -> Dict[str, Any]:
        with self._lock:
            self._refresh()
            root = Path(str(self.config.get("library_dir") or "")).expanduser()
            items = library.scan_library(root, self.config)
            now = utc_iso(utc_now())
            existing = {video["folder"]: video for video in self.state["videos"]}
            seen: set[str] = set()

            for item in items:
                folder = item["folder"]
                seen.add(folder)
                old = existing.get(folder)
                if old is None:
                    item.update(
                        status="pending",
                        slot_date=None,
                        publish_at=None,
                        publish_time_local=None,
                        youtube_id=None,
                        youtube_url=None,
                        uploaded_at=None,
                        uploaded_local_date=None,
                        upload_started_at=None,
                        attempts=0,
                        last_error=None,
                        next_retry_at=None,
                        thumbnail_ok=None,
                        playlist_ok=None,
                        upload_warnings=[],
                        note=None,
                        created_at=now,
                        updated_at=now,
                    )
                    self.state["videos"].append(item)
                    continue

                if old.get("status") == "uploaded":
                    for key in (
                        "folder_path",
                        "video_path",
                        "video_size",
                        "video_size_human",
                        "thumbnail_path",
                        "description_path",
                        "info_path",
                        "av_mismatch",
                        "av_gap",
                    ):
                        old[key] = item.get(key)
                    old["warnings"] = item.get("warnings") or []
                    continue

                keep = {
                    key: old.get(key)
                    for key in (
                        "status",
                        "slot_date",
                        "publish_at",
                        "publish_time_local",
                        "youtube_id",
                        "youtube_url",
                        "uploaded_at",
                        "uploaded_local_date",
                        "attempts",
                        "last_error",
                        "next_retry_at",
                        "note",
                        "created_at",
                    )
                }
                old.update(item)
                old.update({key: value for key, value in keep.items() if value is not None})
                if keep.get("status") in (None, "missing"):
                    old["status"] = "pending"
                else:
                    old["status"] = keep["status"]
                old["updated_at"] = now

            for video in self.state["videos"]:
                if video.get("folder") in seen:
                    continue
                if video.get("status") == "uploaded":
                    continue
                if video.get("status") != "missing":
                    logger.warning("Library item is gone: %s", video.get("folder"))
                video["status"] = "missing"
                video["last_error"] = "Folder or finished video file no longer exists."
                video["updated_at"] = now

            # Recover an upload left behind by a crash / killed process.
            for video in self.state["videos"]:
                if video.get("status") != "uploading":
                    continue
                started = parse_iso(video.get("upload_started_at"))
                if not started or (utc_now() - started) > timedelta(hours=6):
                    video["status"] = "failed"
                    video["last_error"] = "Previous upload was interrupted."
                    video["upload_started_at"] = None
                    video["updated_at"] = now

            self.state["videos"].sort(key=lambda video: str(video.get("folder", "")).lower())
            self._save()
            return self.summary()

    # -- planning ---------------------------------------------------------- #
    def _default_start_date(self, now_local: datetime) -> date:
        start_min, _ = window_minutes(self.config)
        lead = int(self.config.get("upload_lead_minutes") or 90)
        window_start = now_local.replace(
            hour=start_min // 60, minute=start_min % 60, second=0, microsecond=0
        )
        if now_local < window_start - timedelta(minutes=lead):
            return now_local.date()
        return now_local.date() + timedelta(days=1)

    def _next_allowed(self, day: date, used: set[str]) -> date:
        skip = set(int(value) for value in (self.config.get("skip_weekdays") or []))
        if len(skip) >= 7:
            skip = set()
        for _ in range(366 * 5):
            if day.weekday() not in skip and day.isoformat() not in used:
                return day
            day += timedelta(days=1)
        return day

    def _assign_slot(self, video: Dict[str, Any], day: date, tz) -> None:
        start_min, end_min = window_minutes(self.config)
        span = max(1, end_min - start_min)
        rng = random.Random(f"{video.get('folder')}|{day.isoformat()}")
        offset = rng.randrange(0, span)
        local_dt = datetime.combine(day, dtime(0, 0), tzinfo=tz) + timedelta(
            minutes=start_min + offset
        )
        video["slot_date"] = day.isoformat()
        video["publish_time_local"] = local_dt.strftime("%H:%M")
        video["publish_at"] = utc_iso(local_dt)

    def plan(self, start_date: Optional[str] = None, reset: bool = False) -> Dict[str, Any]:
        with self._lock:
            self._refresh()
            local, _ = self._now()
            tz = local.tzinfo
            videos = self.state["videos"]
            # Truncated sources never get a slot: their day is given to the
            # next healthy video, so no calendar day is left empty.
            pending = [
                video
                for video in videos
                if video.get("status") in ("pending", "failed")
                and not video.get("av_mismatch")
            ]
            if reset:
                for video in pending:
                    video.update(
                        slot_date=None,
                        publish_at=None,
                        publish_time_local=None,
                        status="pending",
                        attempts=0,
                        last_error=None,
                        next_retry_at=None,
                        note=None,
                    )

            used = {
                str(video.get("slot_date"))
                for video in videos
                if video.get("slot_date")
                and video.get("status") != "skipped"
                and not video.get("av_mismatch")
                and str(video.get("slot_date")) >= local.date().isoformat()
            }

            if start_date:
                try:
                    cursor = date.fromisoformat(start_date)
                except ValueError as exc:
                    raise ValueError("start_date must be in YYYY-MM-DD format") from exc
            elif used:
                cursor = date.fromisoformat(max(used)) + timedelta(days=1)
            else:
                cursor = self._default_start_date(local)

            todo = [
                video
                for video in pending
                if not video.get("slot_date") or not video.get("publish_at")
            ]
            todo.sort(
                key=lambda video: (
                    str(video.get("source_date") or "9999-99-99"),
                    str(video.get("folder") or "").lower(),
                )
            )

            assigned = 0
            for video in todo:
                cursor = self._next_allowed(cursor, used)
                self._assign_slot(video, cursor, tz)
                video["status"] = "pending"
                video["attempts"] = 0
                video["last_error"] = None
                video["next_retry_at"] = None
                video["note"] = None
                video["updated_at"] = utc_iso(utc_now())
                used.add(cursor.isoformat())
                cursor += timedelta(days=1)
                assigned += 1

            self._save()
            logger.info("Planned %d video(s); next slots start %s", assigned, cursor)
            return {"assigned": assigned, "summary": self.summary()}

    # -- due selection ----------------------------------------------------- #
    def _move_missed(self, video: Dict[str, Any], local: datetime) -> None:
        used = {
            str(other.get("slot_date"))
            for other in self.state["videos"]
            if other is not video
            and other.get("slot_date")
            and other.get("status") in ACTIVE_STATUSES
        }
        new_day = self._next_allowed(local.date() + timedelta(days=1), used)
        self._assign_slot(video, new_day, local.tzinfo)
        video["status"] = "pending"
        video["note"] = f"Missed its slot; moved to {new_day.isoformat()}."
        video["updated_at"] = utc_iso(utc_now())
        logger.warning(
            "Missed slot for %s; moved to %s %s",
            video.get("folder"),
            new_day.isoformat(),
            video.get("publish_time_local"),
        )

    def _find_due_locked(self, local: datetime, now_utc: datetime) -> Tuple[Optional[Dict[str, Any]], Optional[str], bool]:
        lead = timedelta(minutes=int(self.config.get("upload_lead_minutes") or 90))
        candidates = [
            video
            for video in self.state["videos"]
            if video.get("status") in ("pending", "failed") and video.get("publish_at")
        ]
        candidates.sort(key=lambda video: str(video.get("publish_at")))
        changed = False

        for video in candidates:
            if video.get("status") == "failed":
                if int(video.get("attempts") or 0) >= MAX_AUTO_ATTEMPTS:
                    continue
                retry_at = parse_iso(video.get("next_retry_at"))
                if retry_at and now_utc < retry_at:
                    continue

            publish = parse_iso(video.get("publish_at"))
            if publish is None:
                continue

            if publish <= now_utc + timedelta(minutes=10):
                if self.config.get("missed_policy") == "publish_now":
                    return video, "now", changed
                self._move_missed(video, local)
                changed = True
                continue

            upload_at = publish - lead
            if now_utc >= upload_at:
                return video, "scheduled", changed

        return None, None, changed

    def _reserve_next(self, force: bool) -> Tuple[Optional[Dict[str, Any]], Optional[str], str]:
        with self._lock:
            self._refresh()
            local, now_utc = self._now()
            limit = int(self.config.get("max_uploads_per_day") or 1)
            if not force and self._uploads_today() >= limit:
                return None, None, "daily_limit"

            video, mode, changed = self._find_due_locked(local, now_utc)
            if changed:
                self._save()
            if not video:
                return None, None, "idle"

            video["status"] = "uploading"
            video["upload_started_at"] = utc_iso(now_utc)
            video["updated_at"] = utc_iso(now_utc)
            self._save()
            return json.loads(json.dumps(video)), mode, "ok"

    # -- uploads ----------------------------------------------------------- #
    @staticmethod
    def _emit(callback: Optional[EventCallback], event: str, payload: Dict[str, Any]) -> None:
        if not callback:
            return
        try:
            callback(event, payload)
        except Exception:
            logger.exception("Event callback failed")

    def _preview(self, snapshot: Dict[str, Any], mode: str) -> Dict[str, Any]:
        publish_at = snapshot.get("publish_at") if mode == "scheduled" else None
        privacy = "private" if publish_at else "public"
        preview = yt_client.upload_video(
            self.config,
            snapshot,
            publish_at=publish_at,
            privacy=privacy,
            dry_run=True,
        )
        return {
            "status": "dry_run",
            "folder": snapshot.get("folder"),
            "title": snapshot.get("title"),
            "mode": mode,
            "publish_at": publish_at,
            "preview": preview,
        }

    def _block_truncated(
        self,
        snapshot: Dict[str, Any],
        on_event: Optional[EventCallback] = None,
    ) -> Dict[str, Any]:
        """Mark a truncated download as failed without retrying or uploading."""
        folder = str(snapshot.get("folder"))
        title = snapshot.get("title")
        gap = snapshot.get("av_gap")
        message = (
            "Source video is truncated (audio track is "
            f"{gap:.0f} s longer than the video track). YouTube would fail to "
            "process it, so it was not uploaded. Re-download the video, then "
            "retry this folder."
            if isinstance(gap, (int, float))
            else "Source video has mismatched video/audio lengths and was not uploaded."
        )
        now_utc = utc_now()
        with self._lock:
            self._refresh()
            video = self.find(folder)
            if video:
                video.update(
                    status="failed",
                    attempts=MAX_AUTO_ATTEMPTS,  # stop automatic retries
                    last_error=message,
                    next_retry_at=None,
                    upload_started_at=None,
                    thumbnail_ok=False,
                    playlist_ok=False,
                    note="Re-download this video, then retry the folder manually.",
                    updated_at=utc_iso(now_utc),
                )
                self._save()
        logger.error("Skipped truncated source for %s: %s", folder, message)
        payload = {"folder": folder, "title": title, "error": message, "truncated": True}
        self._emit(on_event, "upload_error", payload)
        return {"status": "error", **payload}

    def _execute(
        self,
        snapshot: Dict[str, Any],
        mode: str,
        on_progress: Optional[ProgressCallback] = None,
        on_event: Optional[EventCallback] = None,
    ) -> Dict[str, Any]:
        folder = str(snapshot.get("folder"))
        title = snapshot.get("title")
        cfg = dict(self.config)

        # A truncated source file (audio much longer than video) makes YouTube
        # drop the upload, so never send it: fail it permanently instead.
        if snapshot.get("av_mismatch"):
            return self._block_truncated(snapshot, on_event)

        publish_at = snapshot.get("publish_at") if mode == "scheduled" else None
        privacy = "private" if publish_at else "public"

        self._emit(
            on_event,
            "upload_start",
            {
                "folder": folder,
                "title": title,
                "mode": mode,
                "publish_at": publish_at,
                "privacy": privacy,
            },
        )
        logger.info(
            "Starting upload: %s (%s, publish_at=%s)",
            folder,
            mode,
            publish_at or "immediately",
        )

        try:
            result = yt_client.upload_video(
                cfg,
                snapshot,
                publish_at=publish_at,
                privacy=privacy,
                on_progress=on_progress,
            )
        except yt_client.AuthRequired as exc:
            with self._lock:
                self._refresh()
                video = self.find(folder)
                if video:
                    video["status"] = "pending"
                    video["upload_started_at"] = None
                    video["last_error"] = str(exc)
                    video["updated_at"] = utc_iso(utc_now())
                    self._save()
            logger.error("Authorization required: %s", exc)
            payload = {"folder": folder, "title": title, "error": str(exc)}
            self._emit(on_event, "upload_error", payload)
            return {"status": "auth_required", **payload}
        except Exception as exc:
            now_utc = utc_now()
            message = str(exc) or exc.__class__.__name__
            with self._lock:
                self._refresh()
                video = self.find(folder)
                if video:
                    attempts = int(video.get("attempts") or 0) + 1
                    delay = RETRY_DELAYS_MIN[min(attempts - 1, len(RETRY_DELAYS_MIN) - 1)]
                    video["status"] = "failed"
                    video["attempts"] = attempts
                    video["last_error"] = message
                    video["next_retry_at"] = utc_iso(now_utc + timedelta(minutes=delay))
                    video["upload_started_at"] = None
                    video["updated_at"] = utc_iso(now_utc)
                    self._save()
            logger.error("Upload failed for %s: %s", folder, message)
            payload = {"folder": folder, "title": title, "error": message}
            self._emit(on_event, "upload_error", payload)
            return {"status": "error", **payload}

        if result.get("copyright_blocked"):
            now_utc = utc_now()
            action = result.get("copyright_action") or {}
            deleted = bool(action.get("deleted"))
            message = str(result.get("copyright_message") or "Copyright check failed.")
            with self._lock:
                self._refresh()
                video = self.find(folder)
                if video:
                    video.update(
                        status="failed",
                        attempts=MAX_AUTO_ATTEMPTS,  # stop automatic retries
                        last_error=message,
                        next_retry_at=None,
                        upload_started_at=None,
                        youtube_id=None if deleted else result.get("video_id"),
                        youtube_url=None if deleted else result.get("url"),
                        thumbnail_ok=False,
                        playlist_ok=False,
                        upload_warnings=result.get("warnings") or [],
                        note="Copyright guard: automatic retries are disabled until you retry manually.",
                        updated_at=utc_iso(now_utc),
                    )
                    self._save()
            payload = {
                "folder": folder,
                "title": title,
                "error": message,
                "video_id": None if deleted else result.get("video_id"),
                "url": None if deleted else result.get("url"),
                "deleted": deleted,
            }
            self._emit(on_event, "upload_error", payload)
            logger.error("Copyright block for %s: %s", folder, message)
            return {"status": "error", **payload}

        now_utc = utc_now()
        with self._lock:
            self._refresh()
            video = self.find(folder)
            if video:
                video.update(
                    status="uploaded",
                    youtube_id=result.get("video_id"),
                    youtube_url=result.get("url"),
                    uploaded_at=utc_iso(now_utc),
                    uploaded_local_date=self._now()[0].date().isoformat(),
                    upload_started_at=None,
                    attempts=0,
                    last_error=None,
                    next_retry_at=None,
                    note=None,
                    thumbnail_ok=result.get("thumbnail_ok"),
                    playlist_ok=result.get("playlist_ok"),
                    upload_warnings=result.get("warnings") or [],
                    updated_at=utc_iso(now_utc),
                )
                self.state["last_upload_date"] = video["uploaded_local_date"]
                self._save()

        payload = {
            "folder": folder,
            "title": title,
            "video_id": result.get("video_id"),
            "url": result.get("url"),
            "publish_at": publish_at,
            "privacy": privacy,
            "thumbnail_ok": result.get("thumbnail_ok"),
            "playlist_ok": result.get("playlist_ok"),
            "warnings": result.get("warnings") or [],
        }
        self._emit(on_event, "upload_done", payload)
        logger.info("Upload finished: %s -> %s", folder, result.get("url"))
        return {"status": "uploaded", **payload}

    def upload_next(
        self,
        dry_run: bool = False,
        force: bool = False,
        on_progress: Optional[ProgressCallback] = None,
        on_event: Optional[EventCallback] = None,
    ) -> Dict[str, Any]:
        if not self._upload_lock.acquire(blocking=False):
            return {"status": "busy", "message": "Another upload is already running."}
        try:
            if not self._process_lock.acquire():
                return {
                    "status": "busy",
                    "message": "Another uploader process is already uploading. Try again later.",
                }
            try:
                if dry_run:
                    with self._lock:
                        self._refresh()
                        local, now_utc = self._now()
                        active = [
                            video
                            for video in self.state["videos"]
                            if video.get("status") in ("pending", "failed") and video.get("publish_at")
                        ]
                        active.sort(key=lambda video: str(video.get("publish_at")))
                        if not active:
                            return {
                                "status": "idle",
                                "message": "Nothing is scheduled yet.",
                                "next": None,
                            }
                        future = [
                            video
                            for video in active
                            if (parse_iso(video.get("publish_at")) or now_utc)
                            > now_utc + timedelta(minutes=10)
                        ]
                        video = future[0] if future else active[0]
                        mode = "scheduled" if video in future else "now"
                        snapshot = json.loads(json.dumps(video))
                    return self._preview(snapshot, mode)

                snapshot, mode, reason = self._reserve_next(force)
                if reason == "daily_limit":
                    return {
                        "status": "daily_limit",
                        "message": "The daily upload limit has been reached.",
                        "summary": self.summary(),
                    }
                if not snapshot:
                    return {
                        "status": "idle",
                        "message": "Nothing is due yet.",
                        "next": self.summary().get("next"),
                    }
                return self._execute(snapshot, mode or "scheduled", on_progress, on_event)
            finally:
                self._process_lock.release()
        finally:
            self._upload_lock.release()

    def upload_folder(
        self,
        folder: str,
        dry_run: bool = False,
        on_progress: Optional[ProgressCallback] = None,
        on_event: Optional[EventCallback] = None,
    ) -> Dict[str, Any]:
        if not self._upload_lock.acquire(blocking=False):
            return {"status": "busy", "message": "Another upload is already running."}
        try:
            if not self._process_lock.acquire():
                return {
                    "status": "busy",
                    "message": "Another uploader process is already uploading. Try again later.",
                }
            try:
                with self._lock:
                    self._refresh()
                    video = self.find(folder)
                    if not video:
                        raise KeyError(f"Unknown library folder: {folder}")
                    if video.get("status") == "uploaded":
                        return {
                            "status": "already_uploaded",
                            "folder": folder,
                            "video_id": video.get("youtube_id"),
                            "url": video.get("youtube_url"),
                            "message": "This folder was already uploaded.",
                        }
                    if video.get("status") == "missing":
                        return {
                            "status": "missing",
                            "folder": folder,
                            "message": "The video file for this folder no longer exists.",
                        }
                    if video.get("av_mismatch") and dry_run:
                        gap = video.get("av_gap")
                        detail = f" (audio is {gap:.0f} s longer)" if isinstance(gap, (int, float)) else ""
                        return {
                            "status": "blocked",
                            "folder": folder,
                            "title": video.get("title"),
                            "message": (
                                "Source video is truncated"
                                f"{detail}; YouTube would fail to process it, "
                                "so it will not be uploaded."
                            ),
                            "av_gap": gap,
                        }
                    publish = parse_iso(video.get("publish_at"))
                    mode = "scheduled" if publish and publish > utc_now() + timedelta(minutes=10) else "now"
                    snapshot = json.loads(json.dumps(video))
                    if dry_run:
                        return self._preview(snapshot, mode)
                    snapshot["status"] = "uploading"
                    snapshot["upload_started_at"] = utc_iso(utc_now())
                    snapshot["updated_at"] = utc_iso(utc_now())
                    video.update(
                        status="uploading",
                        upload_started_at=snapshot["upload_started_at"],
                        updated_at=snapshot["updated_at"],
                    )
                    self._save()
                return self._execute(snapshot, mode, on_progress, on_event)
            finally:
                self._process_lock.release()
        finally:
            self._upload_lock.release()

    # -- manual actions ---------------------------------------------------- #
    def reschedule(self, folder: str, day: str, time_str: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            self._refresh()
            video = self.find(folder)
            if not video:
                raise KeyError(f"Unknown library folder: {folder}")
            if video.get("status") == "uploaded":
                raise ValueError("Uploaded videos cannot be rescheduled from here.")
            try:
                target_day = date.fromisoformat(day)
            except ValueError as exc:
                raise ValueError("date must be YYYY-MM-DD") from exc

            local, _ = self._now()
            tz = local.tzinfo
            if time_str:
                try:
                    hour, minute = (int(part) for part in time_str.split(":", 1))
                    local_dt = datetime.combine(target_day, dtime(hour, minute), tzinfo=tz)
                except Exception as exc:
                    raise ValueError("time must be HH:MM") from exc
                video["slot_date"] = target_day.isoformat()
                video["publish_time_local"] = local_dt.strftime("%H:%M")
                video["publish_at"] = utc_iso(local_dt)
            else:
                self._assign_slot(video, target_day, tz)

            publish = parse_iso(video.get("publish_at"))
            if not publish or publish <= utc_now() + timedelta(minutes=5):
                raise ValueError("That publish time is in the past or too soon.")
            video.update(
                status="pending",
                last_error=None,
                next_retry_at=None,
                note=None,
                attempts=0,
                updated_at=utc_iso(utc_now()),
            )
            self._save()
            return {"status": "rescheduled", "video": self._ui_video(video)}

    def skip(self, folder: str) -> Dict[str, Any]:
        with self._lock:
            self._refresh()
            video = self.find(folder)
            if not video:
                raise KeyError(f"Unknown library folder: {folder}")
            if video.get("status") == "uploaded":
                raise ValueError("This video is already uploaded.")
            video.update(
                status="skipped",
                note="Skipped manually.",
                last_error=None,
                next_retry_at=None,
                updated_at=utc_iso(utc_now()),
            )
            self._save()
            return {"status": "skipped", "video": self._ui_video(video)}

    def retry(self, folder: str) -> Dict[str, Any]:
        with self._lock:
            self._refresh()
            video = self.find(folder)
            if not video:
                raise KeyError(f"Unknown library folder: {folder}")
            if video.get("status") == "uploaded":
                raise ValueError("This video is already uploaded.")
            publish = parse_iso(video.get("publish_at"))
            if not publish or publish <= utc_now() + timedelta(minutes=10):
                video["slot_date"] = None
                video["publish_at"] = None
                video["publish_time_local"] = None
            video.update(
                status="pending",
                attempts=0,
                last_error=None,
                next_retry_at=None,
                note="Queued again manually.",
                updated_at=utc_iso(utc_now()),
            )
            self._save()
            return {"status": "pending", "video": self._ui_video(video)}

    # -- settings ---------------------------------------------------------- #
    def update_config(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            merged = dict(self.config)
            merged.update({key: value for key, value in changes.items() if key in appconfig.DEFAULTS})
            self.config = appconfig.save_config(merged)
            self._config_mtime = self._mtime(appconfig.CONFIG_PATH)
        self.scan()
        return appconfig.public_config(self.config)

    def logs_tail(self, limit: int = 150) -> List[str]:
        path = appconfig.LOG_PATH
        if not path.is_file():
            return []
        try:
            with path.open("rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - 128 * 1024))
                text = handle.read().decode("utf-8", "replace")
            return text.splitlines()[-limit:]
        except Exception as exc:
            return [f"Could not read logs: {exc}"]
