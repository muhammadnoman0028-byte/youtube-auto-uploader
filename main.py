"""YouTube auto-uploader: FastAPI dashboard + command line interface.

Common commands
---------------
python main.py auth                     one-time Google OAuth consent
python main.py scan                     index the downloader sub-folders
python main.py plan                     bulk-assign one video per day
python main.py status                   show the plan
python main.py doctor                   validate everything before uploading
python main.py upload-next --dry-run    preview the next upload
python main.py upload-next              upload the next due video
python main.py run                      keep a background scheduler alive
python main.py serve                    open the dashboard at 127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import uvicorn
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

try:  # Windows consoles are often cp1252; never crash on French titles.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import config as appconfig

appconfig.setup_logging()

import yt_client  # noqa: E402
from scheduler import Scheduler, parse_iso, utc_iso  # noqa: E402

logger = logging.getLogger("main")

ROOT = Path(__file__).resolve().parent
INDEX_HTML = ROOT / "index.html"
AUTO_SCHEDULER = False

scheduler = Scheduler()


# --------------------------------------------------------------------------- #
# WebSocket broadcast manager                                                  #
# --------------------------------------------------------------------------- #
class ConnectionManager:
    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)

    async def broadcast(self, frame: Dict[str, Any]) -> None:
        dead: List[WebSocket] = []
        for websocket in list(self._clients):
            try:
                await websocket.send_json(frame)
            except Exception:
                dead.append(websocket)
        for websocket in dead:
            self.disconnect(websocket)

    def broadcast_threadsafe(self, frame: Dict[str, Any]) -> None:
        if self._loop is None or not self._clients:
            return
        asyncio.run_coroutine_threadsafe(self.broadcast(frame), self._loop)


manager = ConnectionManager()
_tasks: Set[asyncio.Task] = set()


def _event_callback() -> Any:
    def callback(event: str, payload: Dict[str, Any]) -> None:
        frame = {"event": event, **payload}
        manager.broadcast_threadsafe(frame)
        if event == "upload_start":
            manager.broadcast_threadsafe(
                {
                    "event": "log",
                    "level": "info",
                    "message": f"Uploading {payload.get('folder')} → "
                    f"{payload.get('publish_at') or 'publish now'}",
                }
            )
        elif event == "upload_done":
            manager.broadcast_threadsafe(
                {
                    "event": "log",
                    "level": "success",
                    "message": f"Uploaded {payload.get('title')} → {payload.get('url')}",
                }
            )
        elif event == "upload_error":
            manager.broadcast_threadsafe(
                {
                    "event": "log",
                    "level": "error",
                    "message": f"Upload failed for {payload.get('folder')}: {payload.get('error')}",
                }
            )

    return callback


def _progress_callback() -> Any:
    last = {"at": 0.0}

    def callback(percent: float, uploaded: int, total: int) -> None:
        now = time.monotonic()
        if percent < 100 and now - last["at"] < 1.0:
            return
        last["at"] = now
        manager.broadcast_threadsafe(
            {
                "event": "upload_progress",
                "percent": round(percent, 1),
                "uploaded": uploaded,
                "total": total,
            }
        )

    return callback


def _track(task: asyncio.Task) -> None:
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _run_upload(folder: Optional[str], dry_run: bool, force: bool) -> None:
    try:
        if folder:
            result = await asyncio.to_thread(
                scheduler.upload_folder,
                folder,
                dry_run,
                _progress_callback(),
                _event_callback(),
            )
        else:
            result = await asyncio.to_thread(
                scheduler.upload_next,
                dry_run,
                force,
                _progress_callback(),
                _event_callback(),
            )
    except Exception as exc:
        logger.exception("Upload task crashed")
        result = {"status": "error", "error": str(exc)}
    try:
        await manager.broadcast({"event": "upload_result", **_jsonable(result)})
        await manager.broadcast({"event": "state", "summary": scheduler.summary()})
    except Exception:
        logger.exception("Could not broadcast upload result")


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


# --------------------------------------------------------------------------- #
# App lifecycle                                                                #
# --------------------------------------------------------------------------- #
async def _safe_scan() -> None:
    try:
        await asyncio.to_thread(scheduler.scan)
    except Exception:
        logger.exception("Initial library scan failed")


async def _daemon_loop() -> None:
    logger.info("Auto-uploader loop started (checks every 60 seconds)")
    while True:
        try:
            if not scheduler.busy:
                result = await asyncio.to_thread(
                    scheduler.upload_next, False, False, None, _event_callback()
                )
                if result.get("status") in ("uploaded", "error", "auth_required", "daily_limit"):
                    await manager.broadcast({"event": "upload_result", **_jsonable(result)})
                    await manager.broadcast({"event": "state", "summary": scheduler.summary()})
        except Exception:
            logger.exception("Auto-uploader loop error")
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(_: FastAPI):
    manager.bind_loop(asyncio.get_running_loop())
    scan_task = asyncio.create_task(_safe_scan())
    daemon_task = asyncio.create_task(_daemon_loop()) if AUTO_SCHEDULER else None
    yield
    for task in (scan_task, daemon_task):
        if task:
            task.cancel()


app = FastAPI(
    title="YouTube Auto Uploader",
    version="1.0.0",
    description="Bulk-schedule a downloader library: one video per day, published "
    "inside a local 20:00-21:00 window.",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Request models                                                               #
# --------------------------------------------------------------------------- #
class PlanRequest(BaseModel):
    start_date: Optional[str] = Field(default=None, description="YYYY-MM-DD")
    reset: bool = False


class UploadRequest(BaseModel):
    folder: Optional[str] = None
    dry_run: bool = False
    force: bool = False


class RescheduleRequest(BaseModel):
    folder: str
    date: str = Field(..., description="YYYY-MM-DD")
    time: Optional[str] = Field(default=None, description="HH:MM (default: random in window)")


class FolderRequest(BaseModel):
    folder: str


# --------------------------------------------------------------------------- #
# API                                                                          #
# --------------------------------------------------------------------------- #
def _state_payload() -> Dict[str, Any]:
    summary = scheduler.summary()
    return {
        "server_time": {
            "local": scheduler._now()[0].isoformat(),
            "utc": utc_iso(scheduler._now()[1]),
        },
        "config": appconfig.public_config(scheduler.config),
        "auth": yt_client.auth_status(scheduler.config),
        "summary": summary,
        "videos": scheduler.ui_videos(),
    }


@app.get("/api/v1/state")
async def get_state() -> Dict[str, Any]:
    return await asyncio.to_thread(_state_payload)


@app.get("/api/v1/health")
async def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "version": app.version,
        "busy": scheduler.busy,
        "auto_scheduler": AUTO_SCHEDULER,
        "library_dir": scheduler.config.get("library_dir"),
    }


@app.get("/api/v1/auth")
async def get_auth() -> Dict[str, Any]:
    return yt_client.auth_status(scheduler.config)


@app.post("/api/v1/scan")
async def scan_library() -> Dict[str, Any]:
    try:
        summary = await asyncio.to_thread(scheduler.scan)
    except NotADirectoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "ok", "summary": summary}


@app.post("/api/v1/plan")
async def plan_videos(request: PlanRequest) -> Dict[str, Any]:
    def _run() -> Dict[str, Any]:
        scheduler.scan()
        return scheduler.plan(start_date=request.start_date, reset=request.reset)

    try:
        result = await asyncio.to_thread(_run)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except NotADirectoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "ok", **result}


@app.post("/api/v1/upload", status_code=202)
async def upload_video(request: UploadRequest) -> Dict[str, Any]:
    if scheduler.busy:
        raise HTTPException(status_code=409, detail="An upload is already running.")
    _track(asyncio.create_task(_run_upload(request.folder, request.dry_run, request.force)))
    return {
        "status": "started",
        "folder": request.folder,
        "dry_run": request.dry_run,
        "ws": "/ws/progress",
    }


@app.post("/api/v1/reschedule")
async def reschedule_video(request: RescheduleRequest) -> Dict[str, Any]:
    try:
        return await asyncio.to_thread(
            scheduler.reschedule, request.folder, request.date, request.time
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/v1/skip")
async def skip_video(request: FolderRequest) -> Dict[str, Any]:
    try:
        return await asyncio.to_thread(scheduler.skip, request.folder)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/v1/retry")
async def retry_video(request: FolderRequest) -> Dict[str, Any]:
    try:
        return await asyncio.to_thread(scheduler.retry, request.folder)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/v1/settings")
async def update_settings(changes: Dict[str, Any]) -> Dict[str, Any]:
    try:
        cfg = await asyncio.to_thread(scheduler.update_config, changes)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "ok", "config": cfg, "summary": scheduler.summary()}


@app.get("/api/v1/thumbnail")
async def thumbnail(folder: str = Query(..., min_length=1)) -> FileResponse:
    video = scheduler.find(folder)
    if not video or not video.get("thumbnail_path"):
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    path = Path(str(video["thumbnail_path"]))
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Thumbnail file is missing")
    return FileResponse(path)


@app.get("/api/v1/logs")
async def get_logs(limit: int = Query(150, ge=1, le=2000)) -> Dict[str, Any]:
    return {"lines": await asyncio.to_thread(scheduler.logs_tail, limit)}


@app.websocket("/ws/progress")
async def ws_progress(websocket: WebSocket) -> None:
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket)


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(INDEX_HTML)


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def _print_summary(summary: Dict[str, Any]) -> None:
    print(
        f"Total {summary['total']}  |  scheduled {summary['scheduled']}  |  "
        f"uploaded {summary['uploaded']}  |  failed {summary['failed']}  |  "
        f"skipped {summary['skipped']}  |  missing {summary['missing']}"
    )
    next_video = summary.get("next")
    if next_video:
        print(
            f"Next: {next_video['slot_date']} {next_video['publish_time_local']} "
            f"(upload at {summary.get('next_upload_at')}) — {next_video['title']}"
        )
    else:
        print("Next: nothing scheduled. Run: python main.py plan")


def _print_status() -> None:
    cfg = scheduler.config
    print(f"Library : {cfg['library_dir']}")
    print(
        f"Window  : {cfg['publish_window_start']}–{cfg['publish_window_end']} "
        f"({cfg['timezone']}), upload {cfg['upload_lead_minutes']} min before publish"
    )
    auth = yt_client.auth_status(cfg)
    print(f"Auth    : {'OK' if auth['authorized'] else 'NOT READY'} — {auth['message']}")
    _print_summary(scheduler.summary())

    print()
    print(f"{'#':>3}  {'DATE':<10}  {'TIME':<5}  {'STATUS':<9}  TITLE")
    videos = scheduler.ui_videos()
    for index, video in enumerate(videos, 1):
        title = (video.get("title") or "")[:70]
        print(
            f"{index:>3}  {str(video.get('slot_date') or '—'):<10}  "
            f"{str(video.get('publish_time_local') or '—'):<5}  "
            f"{str(video.get('status') or '—'):<9}  {title}"
        )


def _cli_event(event: str, payload: Dict[str, Any]) -> None:
    if event == "upload_start":
        print(f"\nUploading: {payload.get('folder')}")
        print(f"Publish at: {payload.get('publish_at') or 'immediately'} ({payload.get('privacy')})")
    elif event == "upload_done":
        print(f"Done: {payload.get('url')}")
    elif event == "upload_error":
        print(f"Error: {payload.get('error')}", file=sys.stderr)


def _print_result(result: Dict[str, Any]) -> None:
    status = result.get("status")
    if status == "uploaded":
        print(f"Uploaded: {result.get('url')}")
        print(f"Publish:  {result.get('publish_at') or 'now'} ({result.get('privacy')})")
        if result.get("warnings"):
            for warning in result["warnings"]:
                print(f"Warning: {warning}")
        return
    if status == "idle":
        print(result.get("message") or "Nothing is due yet.")
        nxt = result.get("next")
        if nxt:
            print(
                f"Next: {nxt.get('slot_date')} {nxt.get('publish_time_local')} — {nxt.get('title')}"
            )
        return
    if status == "daily_limit":
        print(result.get("message") or "Daily upload limit reached.")
        return
    if status == "blocked":
        print(f"Blocked: {result.get('folder')}")
        print(result.get("message") or "This video cannot be uploaded.")
        return
    if status == "dry_run":
        print(f"Dry run: {result.get('folder')}")
        print(json.dumps(result.get("preview", {}).get("payload", {}), ensure_ascii=False, indent=2))
        for warning in result.get("preview", {}).get("warnings") or []:
            print(f"Warning: {warning}")
        return
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if status == "auth_required":
        print("\nAuthenticate first: python main.py auth", file=sys.stderr)


def command_auth() -> int:
    try:
        yt_client.run_oauth(scheduler.config)
    except yt_client.AuthRequired as exc:
        print(f"Auth failed: {exc}", file=sys.stderr)
        return 2
    print(f"Authorized. Token saved to {yt_client.token_path(scheduler.config)}")
    return 0


def command_doctor() -> int:
    cfg = scheduler.config
    root = Path(cfg["library_dir"]).expanduser()
    print(f"Library : {root}  ({'exists' if root.is_dir() else 'MISSING'})")
    if not root.is_dir():
        return 2
    auth = yt_client.auth_status(cfg)
    print(f"Auth    : {'OK' if auth['authorized'] else 'NOT READY'} — {auth['message']}")
    print(f"Secret  : {auth['client_secret']}  ({'found' if auth['client_secret_ok'] else 'missing'})")
    print(
        f"Window  : {cfg['publish_window_start']}–{cfg['publish_window_end']} "
        f"({cfg['timezone']}), lead {cfg['upload_lead_minutes']} min"
    )
    summary = scheduler.scan()
    _print_summary(summary)
    print()

    problems = 0
    for video in scheduler.ui_videos():
        if video.get("status") == "uploaded":
            continue
        warnings = list(video.get("warnings") or [])
        if not video.get("has_thumbnail"):
            warnings.append("No thumbnail")
        if video.get("duration") and int(video["duration"]) > 12 * 3600:
            warnings.append("Longer than YouTube's 12-hour limit")
        flag = "OK " if not warnings else "WARN"
        if warnings:
            problems += 1
        print(
            f"[{flag}] {video.get('slot_date') or '—'} {video.get('publish_time_local') or '—'}  "
            f"{video.get('duration_human'):>8}  {video.get('video_size_human'):>9}  "
            f"{(video.get('title') or '')[:60]}"
        )
        for warning in warnings:
            print(f"        - {warning}")

    print()
    if problems:
        print(f"{problems} item(s) have warnings; they can still upload.")
    print("Doctor finished.")
    return 0


def command_bulk_upload(limit: int, dry_run: bool = False) -> int:
    """Upload pending videos ahead of time, in publish-slot order."""
    scheduler.scan()
    pending = [
        video
        for video in scheduler.state["videos"]
        if video.get("status") == "pending" and video.get("publish_at")
    ]
    pending.sort(key=lambda video: str(video.get("publish_at")))
    if limit and limit > 0:
        pending = pending[:limit]
    if not pending:
        print("No pending videos to upload.")
        return 0

    print(f"Bulk upload: {len(pending)} video(s) queued.")
    uploaded = 0
    for index, video in enumerate(pending, 1):
        folder = str(video.get("folder"))
        print(f"\n[{index}/{len(pending)}] {folder}")
        result = scheduler.upload_folder(folder, dry_run=dry_run, on_event=_cli_event)
        status = result.get("status")
        if dry_run:
            _print_result(result)
            continue
        if status == "uploaded":
            uploaded += 1
            print(f"  OK -> {result.get('url')}")
            continue
        if status in ("already_uploaded", "missing"):
            print(f"  Skipped: {status}")
            continue
        message = str(result.get("error") or result.get("message") or status)
        if result.get("truncated"):
            print(f"  Skipped (truncated source): {message}", file=sys.stderr)
            continue
        print(f"  Stopped: {message}", file=sys.stderr)
        if "quota" in message.lower() or "limit" in message.lower():
            print("YouTube quota/upload limit reached; run again after it resets.", file=sys.stderr)
        break

    print(f"\nUploaded {uploaded} video(s) in this batch.")
    return 0


def command_run(interval: int) -> int:
    print(f"Scheduler running. Checking every {interval}s. Ctrl+C to stop.")
    try:
        while True:
            result = scheduler.upload_next(on_event=_cli_event)
            if result.get("status") not in ("idle", "daily_limit"):
                _print_result(result)
            if result.get("status") == "auth_required":
                print("Stopping: authorization required.", file=sys.stderr)
                return 2
            time.sleep(max(15, interval))
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


def serve(host: str, port: int, with_scheduler: bool) -> None:
    global AUTO_SCHEDULER
    AUTO_SCHEDULER = with_scheduler
    uvicorn.run(app, host=host, port=port, log_level="info")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bulk-schedule and auto-upload a folder-per-video YouTube library."
    )
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("auth", help="run the Google OAuth consent flow")
    sub.add_parser("scan", help="scan the library and update data/state.json")
    sub.add_parser("status", help="print the schedule")

    plan = sub.add_parser("plan", help="bulk-assign one video per day")
    plan.add_argument("--start", dest="start_date", help="first slot date (YYYY-MM-DD)")
    plan.add_argument("--reset", action="store_true", help="clear and rebuild all pending slots")

    upload_next = sub.add_parser("upload-next", help="upload the next due video")
    upload_next.add_argument("--dry-run", action="store_true")
    upload_next.add_argument("--force", action="store_true", help="ignore the daily limit")

    upload = sub.add_parser("upload", help="upload one specific folder now")
    upload.add_argument("--folder", required=True)
    upload.add_argument("--dry-run", action="store_true")

    bulk = sub.add_parser(
        "bulk-upload",
        help="upload pending videos ahead of time (bypasses the daily cap)",
    )
    bulk.add_argument("--limit", type=int, default=0, help="max videos this run (0 = all)")
    bulk.add_argument("--dry-run", action="store_true")

    reschedule = sub.add_parser("reschedule", help="move one video to a specific date")
    reschedule.add_argument("--folder", required=True)
    reschedule.add_argument("--date", required=True, help="YYYY-MM-DD")
    reschedule.add_argument("--time", help="HH:MM (default: random inside the window)")

    run = sub.add_parser("run", help="keep the scheduler alive in this console")
    run.add_argument("--interval", type=int, default=60, help="seconds between checks")

    server = sub.add_parser("serve", help="start the dashboard")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8000)
    server.add_argument("--scheduler", action="store_true", help="also auto-upload from the server")

    sub.add_parser("doctor", help="validate the library and auth before uploading")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    command = args.command or "serve"

    if command == "auth":
        return command_auth()
    if command == "scan":
        summary = scheduler.scan()
        _print_summary(summary)
        return 0
    if command == "plan":
        result = scheduler.plan(start_date=args.start_date, reset=args.reset)
        print(f"Assigned {result['assigned']} video(s).")
        _print_summary(result["summary"])
        return 0
    if command == "status":
        _print_status()
        return 0
    if command == "doctor":
        return command_doctor()
    if command == "upload-next":
        result = scheduler.upload_next(dry_run=args.dry_run, force=args.force, on_event=_cli_event)
        _print_result(result)
        return 2 if result.get("status") in ("auth_required", "error") else 0
    if command == "upload":
        result = scheduler.upload_folder(
            args.folder, dry_run=args.dry_run, on_event=_cli_event
        )
        _print_result(result)
        return 2 if result.get("status") in ("auth_required", "error") else 0
    if command == "bulk-upload":
        return command_bulk_upload(args.limit, args.dry_run)
    if command == "reschedule":
        try:
            result = scheduler.reschedule(args.folder, args.date, args.time)
        except (KeyError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        video = result["video"]
        print(f"Moved to {video['slot_date']} {video['publish_time_local']} — {video['title']}")
        return 0
    if command == "run":
        return command_run(args.interval)
    if command == "serve":
        serve(args.host, args.port, args.scheduler)
        return 0

    build_parser().print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
