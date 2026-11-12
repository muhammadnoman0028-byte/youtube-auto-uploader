"""Re-download library videos whose source file was truncated.

The channel downloader can leave a merged MP4 whose video stream stops before
the audio (bad resume of a partial download). YouTube rejects those uploads, so
this script backs up the broken file and fetches a clean copy, preferring
H.264 + AAC at 720p for maximum player compatibility.

Usage:
    python repair_sources.py              # every flagged video
    python repair_sources.py --id U3mfP8ukjiQ
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import imageio_ffmpeg
import yt_dlp

import config as appconfig
import library

FORMAT = (
    "bv*[vcodec^=avc1][height<=720]+ba[acodec^=mp4a]/"
    "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/"
    "b[height<=720]/b"
)


def _load_state() -> Dict[str, Any]:
    return json.loads(appconfig.STATE_PATH.read_text(encoding="utf-8"))


def _flagged() -> List[Dict[str, Any]]:
    state = _load_state()
    return [video for video in state["videos"] if video.get("av_mismatch")]


def _backup_broken(video_path: Path, stamp: str) -> Path:
    backup = video_path.with_suffix(video_path.suffix + f".truncated-{stamp}.bak")
    video_path.replace(backup)
    return backup


def _remove_fragments(folder: Path) -> None:
    for pattern in ("*.part", "*.ytdl", "*.f*.mp4", "*.f*.webm"):
        for leftover in folder.glob(pattern):
            leftover.unlink(missing_ok=True)


def repair(video: Dict[str, Any], stamp: str) -> bool:
    folder = Path(video["folder_path"])
    video_path = Path(video["video_path"])
    stem = video_path.stem
    info = json.loads(Path(video["info_path"]).read_text(encoding="utf-8"))
    url = info.get("webpage_url") or f"https://www.youtube.com/watch?v={info.get('id')}"
    old_gap = video.get("av_gap")

    print(f"\n== {video['title'][:70]}")
    print(f"   {url}  (audio-video gap: {old_gap} s)")

    if video_path.is_file():
        backup = _backup_broken(video_path, stamp)
        print(f"   backed up broken file -> {backup.name}")
    _remove_fragments(folder)

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    deno = Path(r"D:\Projects\youtube-downloader\bin\deno.exe")
    opts: Dict[str, Any] = {
        "format": FORMAT,
        "merge_output_format": "mp4",
        "outtmpl": str(folder / f"{stem}.%(ext)s"),
        "windowsfilenames": True,
        "writethumbnail": True,
        "writeinfojson": True,
        "writedescription": True,
        "postprocessors": [
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
        ],
        "ffmpeg_location": ffmpeg,
        "overwrites": True,
        "continuedl": False,
        "noprogress": True,
        "quiet": True,
        "no_warnings": False,
        "retries": 5,
        "fragment_retries": 10,
        "socket_timeout": 30,
    }
    if deno.is_file():
        opts["js_runtimes"] = {"deno": {"path": str(deno)}}

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            code = ydl.download([url])
    except Exception as exc:
        print(f"   FAILED: {exc}", file=sys.stderr)
        return False
    if code != 0:
        print(f"   FAILED: yt-dlp exit code {code}", file=sys.stderr)
        return False

    if not video_path.is_file():
        print(f"   FAILED: expected file missing: {video_path}", file=sys.stderr)
        return False

    gap = library.media_gap(video_path)
    size = video_path.stat().st_size
    ok = gap is not None and gap <= library.AV_GAP_TOLERANCE
    print(f"   new size: {size / (1024 * 1024):.1f} MB | gap: {gap:.1f} s | {'OK' if ok else 'STILL BAD'}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", help="YouTube video id to repair")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    videos = _flagged()
    if args.id:
        videos = [video for video in videos if str(video.get("source_id")) == args.id]
    if not videos:
        print("Nothing to repair. Run `python main.py scan` first.")
        return 0

    print(f"Repairing {len(videos)} truncated video(s).")
    for video in videos:
        print(f" - {video['title'][:70]} | gap {video.get('av_gap')} s")
    if args.dry_run:
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    results = [repair(video, stamp) for video in videos]
    print()
    print(f"Repaired {sum(results)}/{len(results)}. Run `python main.py scan` next.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
