"""YouTube Data API v3 authentication + resumable uploads.

One-time setup:
    1. Create a Google Cloud project and enable "YouTube Data API v3".
    2. Configure the OAuth consent screen and add yourself as a test user.
    3. Create an OAuth client ID of type "Desktop app" and download the JSON.
    4. Save it as ``client_secret.json`` next to this file.
    5. Run ``python main.py auth`` once to create ``data/token.json``.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

import config as appconfig
import library

logger = logging.getLogger("youtube")

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]

ProgressCallback = Callable[[float, int, int], None]


class AuthRequired(RuntimeError):
    """Raised when the OAuth token / client secret is missing or unusable."""


def _resolve(path_like: str | Path) -> Path:
    path = Path(path_like)
    return path if path.is_absolute() else appconfig.ROOT / path


def client_secret_path(cfg: Dict[str, Any]) -> Path:
    return _resolve(cfg.get("oauth_client_secret") or "client_secret.json")


def token_path(cfg: Dict[str, Any]) -> Path:
    return _resolve(cfg.get("oauth_token") or "data/token.json")


def auth_status(cfg: Dict[str, Any]) -> Dict[str, Any]:
    secret = client_secret_path(cfg)
    token = token_path(cfg)
    status: Dict[str, Any] = {
        "client_secret": str(secret),
        "client_secret_ok": secret.is_file(),
        "token": str(token),
        "token_ok": token.is_file(),
        "authorized": False,
        "message": "",
    }
    if not secret.is_file():
        status["message"] = (
            "client_secret.json is missing. Create a Desktop OAuth client in Google Cloud "
            "and save its JSON next to main.py."
        )
        return status
    if not token.is_file():
        status["message"] = "Not authorized yet. Run: python main.py auth"
        return status
    try:
        creds = Credentials.from_authorized_user_file(str(token), SCOPES)
    except Exception as exc:
        status["message"] = f"Token could not be read ({exc}). Run: python main.py auth"
        return status
    if creds.valid:
        status["authorized"] = True
        status["message"] = "Connected to YouTube."
    elif creds.expired and creds.refresh_token:
        status["authorized"] = True
        status["message"] = "Token will refresh automatically on the next upload."
    else:
        status["message"] = "Token expired and cannot refresh. Run: python main.py auth"
    return status


def run_oauth(cfg: Dict[str, Any]) -> Credentials:
    secret = client_secret_path(cfg)
    if not secret.is_file():
        raise AuthRequired(
            f"OAuth client secret not found at {secret}. Download a Desktop app OAuth "
            "client JSON from Google Cloud Console and save it there."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    creds = flow.run_local_server(
        port=0,
        open_browser=True,
        prompt="consent",
        access_type="offline",
        success_message="Authorization complete. You can close this window.",
    )
    token = token_path(cfg)
    token.parent.mkdir(parents=True, exist_ok=True)
    appconfig.atomic_write(token, creds.to_json())
    logger.info("OAuth token saved to %s", token)
    return creds


def get_credentials(cfg: Dict[str, Any]) -> Credentials:
    token = token_path(cfg)
    creds: Optional[Credentials] = None
    if token.is_file():
        try:
            creds = Credentials.from_authorized_user_file(str(token), SCOPES)
        except Exception as exc:
            logger.warning("Existing token is unreadable: %s", exc)
            creds = None
    if creds and not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                appconfig.atomic_write(token, creds.to_json())
            except Exception as exc:
                logger.warning("Could not refresh OAuth token: %s", exc)
                creds = None
        else:
            creds = None
    if not creds or not creds.valid:
        raise AuthRequired("YouTube authorization is missing or expired. Run: python main.py auth")
    return creds


def describe_http_error(exc: HttpError) -> str:
    reasons: List[str] = []
    message = ""
    try:
        raw = exc.content.decode("utf-8", "replace") if isinstance(exc.content, bytes) else str(exc.content)
        data = json.loads(raw)
        error = data.get("error") or {}
        message = str(error.get("message") or "")
        reasons = [
            str(item.get("reason"))
            for item in (error.get("errors") or [])
            if item.get("reason")
        ]
    except Exception:
        message = str(exc)

    if "quotaExceeded" in reasons:
        return "YouTube API quota exceeded for today. The uploader will retry tomorrow."
    if "uploadLimitExceeded" in reasons:
        return "This channel has hit its daily upload limit. Try again tomorrow."
    if "youtubeSignupRequired" in reasons:
        return "The Google account has no YouTube channel. Create one first."
    if "forbidden" in reasons and "thumbnail" in message.lower():
        return "Custom thumbnails are not enabled for this channel (verify the channel first)."
    suffix = f" [{', '.join(reasons)}]" if reasons else ""
    return f"{message or exc}{suffix}"


COPYRIGHT_REASONS = {"copyright", "claim"}


def _is_copyright_block(status: Dict[str, Any]) -> bool:
    """True when YouTube rejected the video for copyright reasons."""
    if str(status.get("rejectionReason") or "").lower() in COPYRIGHT_REASONS:
        return True
    failure = status.get("failureReason") or {}
    return str(failure.get("reason") or "").lower() in COPYRIGHT_REASONS


def _video_status(service: Any, video_id: str) -> Dict[str, Any]:
    response = service.videos().list(part="status", id=video_id).execute()
    items = response.get("items") or []
    return dict(items[0].get("status") or {}) if items else {}


def _apply_copyright_policy(
    service: Any, cfg: Dict[str, Any], video_id: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Either delete the blocked video or leave it private as an unscheduled draft."""
    if str(cfg.get("copyright_policy") or "draft").lower() == "delete":
        service.videos().delete(id=video_id).execute()
        logger.error("Copyright block on %s: video deleted", video_id)
        return {"policy": "delete", "deleted": True}

    # Clear publishAt by sending a private status without a publish time.
    status = {
        "privacyStatus": "private",
        "selfDeclaredMadeForKids": bool(payload.get("status", {}).get("selfDeclaredMadeForKids")),
        "license": "youtube",
        "embeddable": True,
    }
    service.videos().update(part="status", body={"status": status}).execute()
    logger.error(
        "Copyright block on %s: kept as a private draft (publishAt cleared)", video_id
    )
    return {"policy": "draft", "deleted": False}


def _wait_for_copyright(
    service: Any, video_id: str, seconds: int, initial: Dict[str, Any], warnings: List[str]
) -> bool:
    """Poll the processing status for a copyright rejection.

    The Data API does not expose Content ID claims/checks, only outright
    rejections, so this is a best-effort guard.
    """
    status = initial or {}
    deadline = time.monotonic() + max(0, seconds)
    while True:
        if _is_copyright_block(status):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(15, remaining))
        try:
            status = _video_status(service, video_id)
        except Exception as exc:
            warnings.append(f"Could not check copyright status: {exc}")
            logger.warning("Copyright status check failed for %s: %s", video_id, exc)
            return False


def _safe_title(item: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    title = re.sub(r"\s+", " ", str(item.get("title") or "Untitled")).strip()
    title = title.replace("<", "").replace(">", "")
    title = library.fit_title(title)
    return title or "Untitled"


def _language(item: Dict[str, Any]) -> str:
    lang = str(item.get("language") or "fr").strip()
    return lang if re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", lang) else "fr"


def build_payload(
    cfg: Dict[str, Any],
    item: Dict[str, Any],
    publish_at: Optional[str] = None,
    privacy: str = "private",
) -> Dict[str, Any]:
    snippet: Dict[str, Any] = {
        "title": _safe_title(item, cfg),
        "description": str(item.get("description") or "")[:5000],
        "categoryId": str(item.get("category_id") or cfg.get("category_id") or "22"),
        "defaultLanguage": _language(item),
        "defaultAudioLanguage": _language(item),
    }
    tags = [str(tag) for tag in (item.get("tags") or []) if str(tag).strip()]
    if tags:
        snippet["tags"] = tags

    status: Dict[str, Any] = {
        "privacyStatus": privacy,
        "selfDeclaredMadeForKids": bool(cfg.get("made_for_kids")),
        "license": "youtube",
        "embeddable": True,
    }
    if publish_at:
        # Scheduled publishing is only allowed on private videos.
        status["privacyStatus"] = "private"
        status["publishAt"] = publish_at
    return {"snippet": snippet, "status": status}


def upload_video(
    cfg: Dict[str, Any],
    item: Dict[str, Any],
    publish_at: Optional[str] = None,
    privacy: str = "private",
    on_progress: Optional[ProgressCallback] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Upload one video (optionally scheduled), set its thumbnail, add to playlist."""
    payload = build_payload(cfg, item, publish_at=publish_at, privacy=privacy)
    warnings: List[str] = []

    thumb_path: Optional[Path] = None
    if item.get("thumbnail_path"):
        try:
            thumb_path, thumb_warnings = library.prepare_thumbnail(item["thumbnail_path"])
            warnings.extend(thumb_warnings)
        except Exception as exc:
            warnings.append(f"Thumbnail skipped: {exc}")

    video_path = Path(str(item.get("video_path") or ""))
    if not video_path.is_file():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    if dry_run:
        return {
            "dry_run": True,
            "payload": payload,
            "video_path": str(video_path),
            "video_size": video_path.stat().st_size,
            "thumbnail_path": str(thumb_path) if thumb_path else None,
            "playlist_id": cfg.get("playlist_id") or None,
            "warnings": warnings,
        }

    creds = get_credentials(cfg)
    service = build("youtube", "v3", credentials=creds, cache_discovery=False)

    mimetype = mimetypes.guess_type(video_path.name)[0] or "video/mp4"
    file_size = video_path.stat().st_size
    media = MediaFileUpload(
        str(video_path),
        mimetype=mimetype,
        chunksize=8 * 1024 * 1024,
        resumable=True,
    )
    request = service.videos().insert(
        part="snippet,status",
        body=payload,
        media_body=media,
        notifySubscribers=bool(cfg.get("notify_subscribers", True)),
    )

    logger.info("Uploading %s (%s)", item.get("folder"), library.human_size(file_size))
    response: Optional[Dict[str, Any]] = None
    try:
        while response is None:
            status, response = request.next_chunk(num_retries=5)
            if status and on_progress:
                total = int(status.total_size or file_size or 0)
                done = int(status.resumable_progress or 0)
                percent = (done / total * 100.0) if total else 0.0
                on_progress(min(percent, 99.9), done, total)
    except HttpError as exc:
        raise RuntimeError(describe_http_error(exc)) from exc

    if not response or not response.get("id"):
        raise RuntimeError("YouTube did not return a video id.")
    video_id = str(response["id"])
    if on_progress:
        on_progress(100.0, file_size, file_size)
    logger.info("Uploaded video id %s", video_id)

    # Copyright guard: YouTube only reports hard rejections through the API.
    check_seconds = int(cfg.get("copyright_check_seconds") or 0)
    if bool(cfg.get("copyright_guard", True)) and check_seconds > 0:
        logger.info("Waiting up to %ss for YouTube's copyright check on %s", check_seconds, video_id)
        blocked = _wait_for_copyright(
            service, video_id, check_seconds, dict(response.get("status") or {}), warnings
        )
        if blocked:
            action = _apply_copyright_policy(service, cfg, video_id, payload)
            message = (
                "YouTube rejected this video for copyright, so it was deleted."
                if action["deleted"]
                else "YouTube rejected this video for copyright, so it was kept as a private draft."
            )
            return {
                "video_id": video_id,
                "url": f"https://youtu.be/{video_id}",
                "privacy": "private",
                "publish_at": None,
                "thumbnail_ok": False,
                "playlist_ok": False,
                "copyright_blocked": True,
                "copyright_action": action,
                "copyright_message": message,
                "warnings": warnings,
                "payload": payload,
            }

    # Custom thumbnail (needs channel verification).
    thumbnail_ok = False
    if thumb_path:
        try:
            thumb_mime = mimetypes.guess_type(thumb_path.name)[0] or "image/jpeg"
            service.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(str(thumb_path), mimetype=thumb_mime, resumable=False),
            ).execute()
            thumbnail_ok = True
            logger.info("Thumbnail set for %s", video_id)
        except HttpError as exc:
            message = describe_http_error(exc)
            warnings.append(f"Thumbnail not set: {message}")
            logger.warning("Thumbnail failed for %s: %s", video_id, message)
        except Exception as exc:
            warnings.append(f"Thumbnail not set: {exc}")
            logger.warning("Thumbnail failed for %s: %s", video_id, exc)

    # Optional playlist.
    playlist_ok = False
    playlist_id = str(cfg.get("playlist_id") or "").strip()
    if playlist_id:
        try:
            service.playlistItems().insert(
                part="snippet",
                body={
                    "snippet": {
                        "playlistId": playlist_id,
                        "resourceId": {"kind": "youtube#video", "videoId": video_id},
                    }
                },
            ).execute()
            playlist_ok = True
        except HttpError as exc:
            message = describe_http_error(exc)
            warnings.append(f"Playlist not updated: {message}")
            logger.warning("Playlist insert failed for %s: %s", video_id, message)
        except Exception as exc:
            warnings.append(f"Playlist not updated: {exc}")

    return {
        "video_id": video_id,
        "url": f"https://youtu.be/{video_id}",
        "privacy": payload["status"]["privacyStatus"],
        "publish_at": publish_at,
        "thumbnail_ok": thumbnail_ok,
        "playlist_ok": playlist_ok,
        "warnings": warnings,
        "payload": payload,
    }
