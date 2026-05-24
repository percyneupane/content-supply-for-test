# Content Supply

A two-stage pipeline that sources short educational clips from TikTok and bulk-uploads them into **Loorio**.

```
TikTok  ──►  Downloader  ──►  downloads/<Category>/*.mp4 + metadata.json  ──►  Uploader  ──►  Loorio
```

- **`Downloader/`** discovers videos by hashtag (pyktok), filters them (views / duration / keyword), downloads with `yt-dlp`, and writes a per-category metadata manifest.
- **`Uploader/`** reads that manifest and runs Loorio's 3-step upload flow for each video, with an idempotency ledger so re-runs skip work already done.

---

## Requirements

- Python 3.9+
- [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) on your PATH (`brew install yt-dlp`)
- Python packages: `pyktok`, `pandas`, `requests`
- A browser logged into TikTok (pyktok borrows its cookies)
- `ffprobe` (optional) — only used by the uploader to recover duration if it is missing from the manifest

```bash
pip install pyktok pandas requests
```

---

## 1. Downloader

`Downloader/dowloader.py` — discover + filter + download, then record metadata.

### Configure

Edit the `SETTINGS` block at the top of the script:

| Setting | Meaning |
|---|---|
| `CATEGORY` | Folder name for this batch, e.g. `Finance`, `SAT`. Output goes to `downloads/<CATEGORY>/`. |
| `HASHTAGS` | Hashtags to search (no `#`). Pooled in order until enough videos are found. |
| `REQUIRED_DOWNLOADS` | How many videos to collect. |
| `MAX_DURATION_SECONDS` | Skip clips longer than this (default 180s — matches Loorio's 3-minute cap). |
| `MIN_VIEWS` | Skip clips below this view count. |
| `BROWSER_NAME` | Browser to borrow TikTok cookies from (`chrome`, `firefox`, `safari`, `edge`). |

### Run

```bash
cd Downloader
python dowloader.py
```

### Output

```
Downloader/downloads/
  <Category>/
    <Safe_Title>-<video_id>.mp4   # the videos
    metadata.json                 # the upload manifest (one record per video)
  downloaded.txt                  # yt-dlp archive, prevents re-downloads
```

Each `metadata.json` is a JSON array. One record:

```json
{
  "video_id": "0000000000000000000",
  "file": "How_rich_people_avoid_taxes-0000000000000000000.mp4",
  "title": "How rich people avoid taxes",
  "description": "3 legal strategies #finance",
  "durationMs": 47000,
  "mimeType": "video/mp4",
  "category": "Finance",
  "source_url": "https://www.tiktok.com/@user/video/0000000000000000000",
  "author_username": "user",
  "author_name": "User",
  "view_count": 12345,
  "like_count": 678,
  "comment_count": 9,
  "share_count": 3,
  "thumbnail_url": "https://...",
  "tiktok_timestamp": 1700000000,
  "downloaded_at": "2026-05-23T00:00:00+00:00"
}
```

`title` and `description` are stored separately because Loorio joins them into a single `caption` at upload time. `fileName`/`fileSize` are **not** stored — the uploader reads them from disk so the file stays the source of truth.

---

## 2. Uploader

`Uploader/upload.py` — manifest-driven bulk upload into Loorio.

### Run

```bash
cd Uploader

# Upload one category folder
python upload.py \
  --path ../Downloader/downloads/Finance \
  --email usera@loorio.test --password password123 \
  --register \
  --topic "Investing"

# Upload every category subfolder under downloads/
python upload.py --path ../Downloader/downloads --all \
  --email usera@loorio.test --password password123 \
  --topic "Investing"

# Preview without calling the API
python upload.py --path ../Downloader/downloads/Finance \
  --email usera@loorio.test --password password123 --dry-run
```

### Flags

| Flag | Default | Purpose |
|---|---|---|
| `--path` | (required) | Folder with videos + `metadata.json` (or a parent, with `--all`). |
| `--email` / `--password` | (required) | Loorio account credentials. |
| `--base` | `http://localhost:3000` | API base URL. **Point this at production when live — nothing else changes.** |
| `--register` | off | Register the account first (tolerates "already exists"). |
| `--topic` | — | Topic name/slug applied to every video this run; resolved to a UUID via `GET /categories`. |
| `--topic-id` | — | Topic UUID used directly (skips lookup; overrides `--topic`). |
| `--category` | — | Disambiguate `--topic` when the topic name isn't unique. |
| `--learning-mode` / `--no-learning-mode` | **on** | Sets `learningMode` on each video. On by default for educational clips. |
| `--username` / `--bio` | email local part / empty | Used only if a profile must be created. |
| `--all` | off | Treat `--path` as a parent and walk category subfolders. |
| `--limit N` | 0 (no limit) | Cap uploads per folder. |
| `--dry-run` | off | List what would upload without touching the API. |

### What it does per video

1. **Login** → bearer token; ensure a profile exists (creates one if not).
2. **Resolve topic** (once) — `--topic` name → UUID via `GET /categories`.
3. For each manifest record:
   - `POST /videos/upload/init` `{fileName, mimeType, fileSize, durationMs?}` → `{uploadUrl, s3Key, publicUrl}`
   - `PUT <uploadUrl>` — raw video bytes (presigned storage upload)
   - `POST /videos/upload/complete` `{s3Key, url, caption, durationMs, sizeBytes, mimeType, learningMode, topicId, thumbnailUrl}` → `{id}`
   - Record the new video id in `uploaded.json`.

### Idempotency

Each folder gets an `uploaded.json` ledger keyed by `video_id`:

```json
{
  "0000000000000000000": {
    "videoId": "loorio-uuid",
    "caption": "How rich people avoid taxes",
    "uploadedAt": "2026-05-23T00:00:00+00:00"
  }
}
```

Re-running skips anything already in the ledger, so the uploader is safe to run repeatedly.

---

## Field mapping (Content Supply → Loorio)

Loorio's upload DTO does **not** have separate title/description/category fields. The mapping:

| Manifest field | Loorio `complete` field |
|---|---|
| `title` + `description` | `caption` (joined `title\ndescription`, max 1000 chars) |
| *(category folder)* | not stored on the video — you set `topicId` instead (a topic belongs to a category) |
| `--topic` → UUID | `topicId` |
| `--learning-mode` | `learningMode` |
| `durationMs` | `durationMs` (omitted if unknown; DTO requires ≥1) |
| file size on disk | `sizeBytes` |
| `mimeType` | `mimeType` |
| `thumbnail_url` | `thumbnailUrl` |

Init limits enforced by the API: file size ≤ 200 MB, duration ≤ 180 000 ms (3 min).

---

## Notes & caveats

- **Presigned PUT** — the storage upload (step 2) is implemented as a `PUT` of raw bytes, the standard presigned contract. The Postman collection skips this step, so confirm the method against your storage backend if uploads fail there.
- **Thumbnails** — the uploader passes the TikTok CDN `thumbnail_url` as `thumbnailUrl`. The mobile app instead generates and uploads its own thumbnail; TikTok URLs can expire, so revisit this before production.
- **Backfill** — videos downloaded before the metadata feature have no `metadata.json` and can't be uploaded until backfilled.
- **Local API** — defaults assume the Loorio backend runs at `http://localhost:3000`. If `--topic` resolution fails, it lists the available topics from `GET /categories`.
