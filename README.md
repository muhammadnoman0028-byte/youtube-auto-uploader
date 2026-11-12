# YouTube Auto Uploader (Daily Scheduler)

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![YouTube Data API v3](https://img.shields.io/badge/YouTube%20Data%20API-v3-FF0000?logo=youtube&logoColor=white)
![License: MIT](https://img.shields.io/badge/License-MIT-green)
![Platform: Windows](https://img.shields.io/badge/Platform-Windows-0078D6?logo=windows&logoColor=white)

Takes a folder of finished videos (one sub-folder per video) and turns them into
a hands-off YouTube publishing schedule:

```
YouTube_Downloads/<Video Title>/
    <Video Title>.mp4        -> uploaded video
    thumbnail.jpg            -> custom thumbnail
    <Video Title>.description-> description
    <Video Title>.info.json  -> title / tags / category / language
```

- **Bulk planning** – one click assigns *every* pending video to its own day.
- **One upload per day** – the scheduler uploads at most one video daily.
- **Default publish window 20:00–21:00** – each day gets a random minute inside
  that window (still one video per day).
- **Uploads before publish** – by default 90 minutes early (18:30–19:30), so
  YouTube publishes automatically at the planned time.
- **Private + `publishAt`** – videos are uploaded as private scheduled
  uploads; YouTube flips them public at the slot time.
- Resumes safely after restarts, retries failures, and moves missed slots to
  the next free day instead of publishing late.

---

## 1. One-time Google setup

1. Open <https://console.cloud.google.com/> and create (or select) a project.
2. **APIs & Services → Library → YouTube Data API v3 → Enable**.
3. **APIs & Services → OAuth consent screen**:
   - User type **External** (or Internal if you use Workspace).
   - Add your Google account under **Test users** while the app is in testing.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type: **Desktop app**.
   - Download the JSON.
5. Save the downloaded file as:

```
D:\Projects\yt-auto-uploader\client_secret.json
```

6. Install dependencies and authorize once:

```powershell
cd D:\Projects\yt-auto-uploader
pip install -r requirements.txt
python main.py auth
```

A browser window opens; pick the channel's Google account and accept. The token
is stored in `data/token.json` and refreshes itself from then on.

> The account must already have a YouTube channel. Custom thumbnails also
> require channel verification (phone verification) in YouTube Studio — the
> API cannot bypass that, but the video still uploads if the thumbnail fails.

---

## 2. First run

```powershell
cd D:\Projects\yt-auto-uploader

# read the downloader folders
python main.py scan

# assign every pending video to one day, starting tomorrow
python main.py plan

# inspect the full plan
python main.py status

# validate titles, thumbnails, descriptions, auth
python main.py doctor

# preview the next upload without touching YouTube
python main.py upload-next --dry-run
```

Default window: **20:00–21:00 local time**, one video per day, upload 90 minutes
before publish. Change `library_dir`, window, timezone, tags, playlist, etc. in
`config.json` or in the dashboard settings.

---

## 3. Automation (pick one)

### A. Windows Task Scheduler — recommended

Registers a task that starts at **18:00** and repeats **every 30 minutes for 4
hours**. It uploads the day's video once its 90-minute lead time arrives (the
20:00–21:00 publish window maps to 18:30–19:30), and later runs do nothing
because of the one-upload-per-day cap:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_task.ps1
# or a different start / cadence:
powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -At 18:00 -EveryMinutes 15 -WindowHours 4
```

The task uses **StartWhenAvailable**, so if the PC was off during the window it
runs as soon as possible. If the publish time has already passed, the video is
moved to the next free day (configurable with `missed_policy`). Logs go to
`data\uploader.log`.

Remove it with `.\uninstall_task.ps1`.

#### Clearing a backlog

The daily task only uploads the current day's video. To upload a large backlog
quickly, register the bulk task as well — it runs daily at **13:10** (just after
the YouTube quota resets at 13:00 local) and uploads up to 6 already-scheduled
videos per day:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_bulk_task.ps1
powershell -ExecutionPolicy Bypass -File .\install_bulk_task.ps1 -Limit 4 -At 13:15
```

Videos keep their scheduled private `publishAt` times, so publishing stays one
per day. Remove it with
`Unregister-ScheduledTask -TaskName "YouTube Bulk Uploader" -Confirm:$false`.

### B. Keep a scheduler daemon running

```powershell
python main.py run
```

Checks every 60 seconds and uploads when the next video becomes due. Stop with
Ctrl+C.

### C. Dashboard with built-in scheduler

```powershell
python main.py serve --scheduler
```

Open <http://127.0.0.1:8000>. The server both shows the plan and auto-uploads
while it is running. Without `--scheduler` it is display/manual-only.

> Run only one automatic uploader at a time. A cross-process lock prevents two
> simultaneous uploads, but the simplest setup is Task Scheduler **or** the
> daemon, not both.

---

## 4. How bulk scheduling works

`python main.py plan` (or **Schedule all** in the dashboard):

1. Scans every sub-folder with a finished video (leftover `.f137.mp4` /
   `.part` fragments are ignored, so interrupted downloads are not scheduled).
2. Sorts pending videos by their original upload date.
3. Assigns consecutive calendar days, skipping any `skip_weekdays`.
4. Picks a random minute inside `20:00–21:00` for each video.
5. Stores the plan in `data/state.json` — no YouTube API calls yet.

The uploader then, once per day, uploads the day's video as **private** with
`publishAt` set, plus:

- title, description and tags from `.info.json` / `.description`
- `thumbnail.jpg` (re-encoded to ≤2 MB / 16:9 when needed)
- source category mapped to YouTube category IDs, fallback `22` (People & Blogs)
- language (`fr-FR` etc.), optional playlist, made-for-kids flag

YouTube publishes it automatically at the exact slot time.

### Title and copyright safeguards

- Titles are left exactly as they are unless they exceed YouTube's 100-character
  limit. In that case a trailing `| Boris Cyrulnik` (also `– Boris Cyrulnik
  explique`) is removed first; the rest of the title is untouched. Only if it is
  still too long is it truncated at a word boundary.
- After uploading, the uploader polls YouTube's processing status for up to
  `copyright_check_seconds` (120). If YouTube *rejects* the video for copyright,
  `copyright_policy` applies: `draft` (default) clears `publishAt` and leaves it
  private, `delete` removes it. The video is marked failed and automatic retries
  stop, so it is never published.
- API limitation: the Data API reports hard copyright **rejections**, not
  Content ID **claims**. A claimed video can still publish with a claim; review
  YouTube Studio → Copyright for those.

### Truncated downloads

Some downloaded MP4s have a video track that ends long before the audio track
(a bad resume of a partial download). YouTube cannot process those and removes
the upload after 10–30 minutes.

- Every scan compares the video and audio stream lengths with PyAV. A gap over
  10 seconds sets `av_mismatch`, adds a warning, and **blocks the upload** (the
  folder is marked skipped/failed instead of being sent).
- Truncated folders are left out of the plan, so the next healthy video takes
  their day — the schedule stays one video per day with no empty dates.
- Fix the files with `python repair_sources.py` (backs up the broken MP4 and
  re-downloads H.264 + AAC), then `python main.py scan` and `python main.py plan`
  to put them back on the calendar.

### Quota

YouTube gives 10,000 API units/day. One upload costs **1,600**, a thumbnail
**50**, a playlist insert **50**. So the hard maximum is about **6 uploads/day**
on a fresh quota. The default is **1/day**, which leaves plenty of headroom.

`max_uploads_per_day` is a hard cap for automatic uploads. Manual “Upload now”
buttons ignore it (use them deliberately).

### Missing a slot

- `missed_policy: "reschedule"` (default) — the video moves to the next free day;
  the rest of the plan stays one-per-day.
- `missed_policy: "publish_now"` — upload it publicly immediately when the
  uploader next runs.
- Manual **Move** in the dashboard reschedules any pending video.
- Failed uploads retry with backoff (15m, 1h, 4h, 12h, 24h) up to 5 attempts;
  **Retry** resets the counter.

---

## 5. Dashboard

```powershell
python main.py serve
```

- **Scan library** – pick up new downloader folders.
- **Schedule all** – bulk-assign every pending video, one per day.
- **Upload due** – run the uploader now (uploads only if something is due).
- Per-video **Upload now / Move / Retry / Skip**.
- Live upload progress over WebSocket, activity log, auth and quota status.
- Settings drawer: library folder, timezone, publish window, upload lead,
  category, playlist, extra tags, title prefix/suffix, description footer,
  missed-slot policy, skip weekdays.

The dashboard re-plans automatically only when you press **Save & re-plan**;
changing the window alone does not move existing slots.

---

## 6. Commands

| Command | What it does |
|---|---|
| `python main.py auth` | Google OAuth consent, saves `data/token.json` |
| `python main.py scan` | Index downloader sub-folders into `data/state.json` |
| `python main.py plan` | Bulk-assign one video per day |
| `python main.py plan --reset` | Clear pending slots and rebuild them |
| `python main.py plan --start 2026-12-01` | Start the plan on a specific date |
| `python main.py status` | Print the whole schedule |
| `python main.py doctor` | Preflight titles/thumbnails/descriptions/auth |
| `python main.py upload-next` | Upload the next due video |
| `python main.py upload-next --dry-run` | Print the exact API payload |
| `python main.py upload --folder "..."` | Upload one folder now |
| `python main.py bulk-upload --limit 4` | Upload pending videos ahead of time (ignores daily cap; quota-bound) |
| `python repair_sources.py` | Re-download truncated source files (backs up the broken MP4) |
| `python main.py reschedule --folder "..." --date 2026-12-05 --time 20:30` | Move one video |
| `python main.py run` | Standalone every-60s scheduler |
| `python main.py serve` | Dashboard at 127.0.0.1:8000 |
| `python main.py serve --scheduler` | Dashboard + auto-uploader |

---

## 7. Config reference (`config.json`)

| Key | Default | Meaning |
|---|---|---|
| `library_dir` | `D:/Projects/YouTube_Downloads` | Downloader root |
| `timezone` | `local` | `local` or IANA name (`Europe/Paris`, `Africa/Casablanca`) |
| `publish_window_start` | `20:00` | Window opens |
| `publish_window_end` | `21:00` | Window closes (random minute inside) |
| `upload_lead_minutes` | `90` | Upload this early |
| `max_uploads_per_day` | `1` | Automatic upload cap |
| `skip_weekdays` | `[]` | `0`=Mon … `6`=Sun |
| `category_id` | `22` | Fallback YouTube category |
| `default_language` | `fr` | Fallback video language |
| `playlist_id` | `""` | Optional playlist to add uploads to |
| `made_for_kids` | `false` | `selfDeclaredMadeForKids` |
| `extra_tags` | `[]` | Appended to source tags |
| `max_tags` | `15` | Per-video tag cap |
| `title_prefix` / `title_suffix` | `""` | Decorate titles |
| `description_footer` | `""` | Appended to every description |
| `missed_policy` | `reschedule` | `reschedule` or `publish_now` |
| `copyright_guard` | `true` | Poll for copyright rejection after upload |
| `copyright_policy` | `draft` | On rejection: keep private (`draft`) or `delete` |
| `copyright_check_seconds` | `120` | How long to wait for the check (0 disables) |
| `notify_subscribers` | `true` | YouTube notification bell |
| `oauth_client_secret` | `client_secret.json` | OAuth client file |
| `oauth_token` | `data/token.json` | Saved refresh token |

---

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `Auth required` | `python main.py auth` (browser must open; sign in as the channel owner) |
| `client_secret.json missing` | Create a **Desktop app** OAuth client and save its JSON with that exact name |
| `Access blocked: app not verified` / `access_denied` | Add your Google account under OAuth consent screen → **Test users** |
| `quotaExceeded` | Daily 10,000-unit limit; wait for midnight Pacific time |
| `uploadLimitExceeded` | Channel's own daily upload cap; wait a day |
| Thumbnail not set | Verify the channel for custom thumbnails in YouTube Studio |
| Video uploaded but still private | Check `publishAt` is in the future and the uploader clock/timezone is right |
| Wrong publish time | Set `timezone` explicitly (e.g. `Africa/Casablanca`) instead of `local` |
| A video is stuck `uploading` | It is reset to `failed` automatically after 6 hours; use **Retry** |
| `Task Scheduler` does nothing | Run `python main.py upload-next` manually and check `data\uploader.log` |

---

## 9. Project layout

| File | Role |
|---|---|
| `main.py` | FastAPI dashboard, REST API, WebSocket, CLI |
| `scheduler.py` | State, bulk planning, due selection, daily upload engine |
| `yt_client.py` | OAuth, resumable upload, thumbnail, playlist |
| `library.py` | Sub-folder scanning, metadata, thumbnail validation |
| `config.py` | Config/state paths, logging, atomic writes |
| `index.html` | Dark dashboard (no build step) |
| `install_task.ps1` | Registers the daily Windows task |
| `install_bulk_task.ps1` | Registers the daily quota-limited backlog task |
| `data/state.json` | The plan + upload history |
| `data/token.json` | OAuth refresh token (keep private) |
| `data/uploader.log` | Rotating log |

> Only upload content you own or are authorized to publish. Bulk re-uploading
> someone else's channel can lead to copyright strikes on the destination
> channel.
