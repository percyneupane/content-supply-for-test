import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyktok as pyk


# ==========================
# SETTINGS
# ==========================

CATEGORY = "Finance"

# TikTok hashtags to search. Do not include the # symbol.
# The script iterates through these in order, pooling discovered videos
# until REQUIRED_DOWNLOADS is reached.
HASHTAGS = ["finance"]


REQUIRED_DOWNLOADS = 5

MAX_DURATION_SECONDS = 180
MIN_VIEWS = 10_000

SCRIPT_DIR = Path(__file__).parent
BASE_DOWNLOAD_DIR = SCRIPT_DIR / "downloads"
CATEGORY_DIR = BASE_DOWNLOAD_DIR / CATEGORY

TEMP_DIR = SCRIPT_DIR / "temp"

# Per-category manifest written next to the downloaded videos. This is the
# bulk-upload source of truth consumed by Uploader/upload.py.
METADATA_FILENAME = "metadata.json"
UPLOAD_MIME_TYPE = "video/mp4"

# Browser to borrow TikTok cookies from. Must be a browser you are logged
# into TikTok with. Options: "chrome", "firefox", "safari", "edge".
BROWSER_NAME = "chrome"

SLEEP_BETWEEN_DOWNLOADS = 1
SUBPROCESS_TIMEOUT_SECONDS = 120


# ==========================
# BASIC HELPERS
# ==========================

def create_folder(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def safe_print(text: str):
    print(text, flush=True)


def run_command(command):
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        safe_print(f"Command timed out after {SUBPROCESS_TIMEOUT_SECONDS}s: {' '.join(command[:2])}")
        return subprocess.CompletedProcess(command, returncode=1, stdout="", stderr="timeout")

    if result.returncode != 0:
        safe_print(result.stderr)

    return result


def clean_filename(text: str) -> str:
    text = re.sub(r"[^\w\s.-]", "", text)
    text = re.sub(r"\s+", "_", text)
    return text[:80] or "tiktok_video"


# ==========================
# PYKTOK DISCOVERY
# ==========================

def discover_tiktok_urls_from_hashtag(hashtag: str):
    """
    Uses Pyktok to collect TikTok URLs from a hashtag page.
    Pyktok usually returns around 15-30 videos depending on TikTok response.
    """

    create_folder(TEMP_DIR)
    metadata_file = TEMP_DIR / f"{hashtag}_metadata.csv"

    safe_print("\n" + "=" * 60)
    safe_print("DISCOVERING TIKTOK VIDEOS WITH PYKTOK")
    safe_print("=" * 60)
    safe_print(f"Hashtag: #{hashtag}")
    safe_print(f"Metadata file: {metadata_file}")

    if metadata_file.exists():
        metadata_file.unlink()

    try:
        # Borrow TikTok cookies from a logged-in browser; without this,
        # Pyktok almost always returns empty results.
        pyk.specify_browser(BROWSER_NAME)

        # save_video=False because yt-dlp will handle downloading.
        pyk.save_tiktok_multi_page(
            hashtag,
            ent_type="hashtag",
            save_video=False,
            metadata_fn=str(metadata_file)
        )
    except Exception as error:
        safe_print("Pyktok discovery failed.")
        safe_print(str(error))
        safe_print("Try a different hashtag, updating pyktok, or running with browser visible if supported.")
        return []

    if not metadata_file.exists():
        safe_print("No metadata file was created by Pyktok.")
        return []

    df = pd.read_csv(metadata_file)

    safe_print(f"Pyktok columns found: {list(df.columns)}")
    safe_print(f"Rows found: {len(df)}")

    urls = extract_urls_from_dataframe(df)

    safe_print(f"TikTok URLs extracted: {len(urls)}")

    return urls


def extract_urls_from_dataframe(df: pd.DataFrame):
    """
    Reconstructs TikTok URLs from Pyktok metadata.

    Newer Pyktok versions return columns like 'video_id' and 'author_username'
    instead of a full URL column, so we build the canonical URL ourselves.
    Falls back to scanning string cells for any embedded tiktok.com/video/ URLs.
    """

    seen = set()
    urls = []

    has_id = "video_id" in df.columns
    has_user = "author_username" in df.columns

    if has_id and has_user:
        for _, row in df.iterrows():
            video_id = row.get("video_id")
            username = row.get("author_username")

            if pd.isna(video_id) or pd.isna(username):
                continue

            url = f"https://www.tiktok.com/@{username}/video/{int(video_id) if str(video_id).isdigit() else video_id}"

            if url not in seen:
                seen.add(url)
                urls.append(url)

    # Fallback: also scan every string cell for embedded URLs.
    for _, row in df.iterrows():
        for value in row.values:
            if not isinstance(value, str):
                continue

            if "tiktok.com" in value and "/video/" in value:
                cleaned = value.strip()
                if cleaned not in seen:
                    seen.add(cleaned)
                    urls.append(cleaned)

    return urls


# ==========================
# YT-DLP METADATA + DOWNLOAD
# ==========================

def get_ytdlp_metadata(url: str):
    """
    Uses yt-dlp to read TikTok metadata before downloading.
    """

    safe_print("\n" + "-" * 60)
    safe_print("READING VIDEO METADATA WITH YT-DLP")
    safe_print("-" * 60)
    safe_print(f"URL: {url}")

    command = [
        "yt-dlp",
        url,
        "--dump-json",
        "--skip-download",
        "--ignore-errors",
        "--no-warnings",
    ]

    result = run_command(command)

    if not result.stdout.strip():
        safe_print("Skipped: yt-dlp could not read metadata.")
        return None

    try:
        return json.loads(result.stdout.splitlines()[0])
    except Exception:
        safe_print("Skipped: invalid yt-dlp metadata.")
        return None


def explain_skip_reason(video, category: str, hashtag: str):
    title = video.get("title") or ""
    description = video.get("description") or ""
    duration = video.get("duration")
    views = video.get("view_count")

    searchable_text = f"{title.lower()} {description.lower()}"
    category_lower = category.lower()
    hashtag_lower = hashtag.lower()

    if duration is None:
        return "Skipped: missing duration"

    if duration > MAX_DURATION_SECONDS:
        return f"Skipped: too long ({duration}s > {MAX_DURATION_SECONDS}s)"

    # TikTok metadata often omits view_count; treat missing as 0 so the
    # filter doesn't silently discard otherwise-valid videos.
    effective_views = views if isinstance(views, int) else 0

    if effective_views < MIN_VIEWS:
        return f"Skipped: not enough views ({effective_views} < {MIN_VIEWS})"

    # Use word boundaries so "sat" doesn't match "saturday"/"satisfied".
    category_pattern = re.compile(rf"\b{re.escape(category_lower)}\b")
    hashtag_pattern = re.compile(rf"\b{re.escape(hashtag_lower)}\b")

    if not category_pattern.search(searchable_text) and not hashtag_pattern.search(searchable_text):
        return "Skipped: category/hashtag not found in title or description"

    return "Passed"


# ==========================
# METADATA MANIFEST
# ==========================

def build_metadata_record(url: str, video, category: str, file_name: str) -> dict:
    """
    Normalizes the yt-dlp metadata into the exact shape the Loorio bulk
    uploader needs, plus extra fields kept for later (stats, author, source).

    title + description are stored separately because Loorio's uploader joins
    them into a single `caption` (max 1000 chars), exactly like the mobile app.
    fileName/fileSize are read from disk at upload time, so they are not stored.
    """

    duration = video.get("duration")
    duration_ms = int(duration * 1000) if isinstance(duration, (int, float)) else None

    title = (video.get("title") or "").strip()
    description = (video.get("description") or "").strip()

    return {
        "video_id": str(video.get("id") or ""),
        "file": file_name,
        "title": title,
        "description": description,
        "durationMs": duration_ms,
        "mimeType": UPLOAD_MIME_TYPE,
        "category": category,
        "source_url": video.get("webpage_url") or url,
        "author_username": video.get("uploader") or video.get("uploader_id"),
        "author_name": video.get("creator") or video.get("channel"),
        "view_count": video.get("view_count"),
        "like_count": video.get("like_count"),
        "comment_count": video.get("comment_count"),
        "share_count": video.get("repost_count"),
        "thumbnail_url": video.get("thumbnail"),
        "tiktok_timestamp": video.get("timestamp"),
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
    }


def upsert_metadata_record(category_dir: Path, record: dict):
    """
    Appends (or replaces by video_id) a record in the category manifest.
    Builds a new list rather than mutating the loaded one.
    """

    manifest_path = category_dir / METADATA_FILENAME

    existing = []
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            safe_print("Existing manifest unreadable, starting a fresh one.")
            existing = []

    video_id = record.get("video_id")
    others = [r for r in existing if r.get("video_id") != video_id]
    updated = others + [record]

    manifest_path.write_text(
        json.dumps(updated, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    safe_print(f"Metadata saved: {manifest_path} ({len(updated)} records)")


def download_with_ytdlp(url: str, video):
    create_folder(CATEGORY_DIR)

    title = video.get("title") or "Untitled"
    video_id = video.get("id") or "unknown"
    views = video.get("view_count")
    duration = video.get("duration")

    safe_title = clean_filename(title)

    safe_print("\n" + "=" * 60)
    safe_print("DOWNLOADING WITH YT-DLP")
    safe_print("=" * 60)
    safe_print(f"Title: {title}")
    safe_print(f"Views: {views}")
    safe_print(f"Duration: {duration}s")
    safe_print(f"URL: {url}")

    command = [
        "yt-dlp",
        url,

        # Best available TikTok video.
        "-f",
        "best",

        "--merge-output-format",
        "mp4",

        # Avoid duplicates.
        "--download-archive",
        str(BASE_DOWNLOAD_DIR / "downloaded.txt"),

        # Video only, no thumbnail/metadata sidecar files.
        "-o",
        str(CATEGORY_DIR / f"{safe_title}-{video_id}.%(ext)s"),
    ]

    result = subprocess.run(command)

    if result.returncode == 0:
        safe_print("Download result: success")

        # --merge-output-format mp4 guarantees the final extension.
        file_name = f"{safe_title}-{video_id}.mp4"
        record = build_metadata_record(url, video, CATEGORY, file_name)
        upsert_metadata_record(CATEGORY_DIR, record)

        return True

    safe_print("Download result: failed")
    return False


# ==========================
# MAIN PROGRAM
# ==========================

def main():
    create_folder(BASE_DOWNLOAD_DIR)
    create_folder(CATEGORY_DIR)
    create_folder(TEMP_DIR)

    safe_print("\nStarting TikTok Pyktok + yt-dlp downloader")
    safe_print(f"Category: {CATEGORY}")
    safe_print(f"Hashtags ({len(HASHTAGS)}): {', '.join('#' + h for h in HASHTAGS)}")
    safe_print(f"Required downloads: {REQUIRED_DOWNLOADS}")
    safe_print(f"Max duration: {MAX_DURATION_SECONDS}s")
    safe_print(f"Minimum views: {MIN_VIEWS}")
    safe_print(f"Output folder: {CATEGORY_DIR}")

    downloaded_count = 0
    seen_urls = set()

    for hashtag_index, hashtag in enumerate(HASHTAGS, start=1):
        if downloaded_count >= REQUIRED_DOWNLOADS:
            break

        safe_print("\n" + "#" * 60)
        safe_print(f"HASHTAG {hashtag_index}/{len(HASHTAGS)}: #{hashtag}")
        safe_print(f"Downloaded so far: {downloaded_count}/{REQUIRED_DOWNLOADS}")
        safe_print("#" * 60)

        urls = discover_tiktok_urls_from_hashtag(hashtag)

        if not urls:
            safe_print(f"No URLs found for #{hashtag}, moving on.")
            continue

        for index, url in enumerate(urls, start=1):
            if downloaded_count >= REQUIRED_DOWNLOADS:
                break

            if url in seen_urls:
                safe_print(f"Skipped duplicate URL (already seen in another hashtag): {url}")
                continue

            seen_urls.add(url)

            safe_print("\n" + "=" * 60)
            safe_print(f"PROCESSING URL {index}/{len(urls)}  (hashtag #{hashtag})")
            safe_print("=" * 60)

            video = get_ytdlp_metadata(url)

            if video is None:
                continue

            title = video.get("title") or "Untitled"
            views = video.get("view_count")
            duration = video.get("duration")

            safe_print("\nFILTERING")
            safe_print(f"Title: {title}")
            safe_print(f"Views: {views}")
            safe_print(f"Duration: {duration}s")

            reason = explain_skip_reason(video, CATEGORY, hashtag)
            safe_print(reason)

            if reason == "Passed":
                success = download_with_ytdlp(url, video)

                if success:
                    downloaded_count += 1
                    safe_print(f"Progress: {downloaded_count}/{REQUIRED_DOWNLOADS} downloaded")

                    if downloaded_count < REQUIRED_DOWNLOADS:
                        safe_print(f"Waiting {SLEEP_BETWEEN_DOWNLOADS} seconds...")
                        time.sleep(SLEEP_BETWEEN_DOWNLOADS)

    safe_print("\n" + "=" * 60)
    safe_print("FINISHED")
    safe_print("=" * 60)
    safe_print(f"Downloaded: {downloaded_count}/{REQUIRED_DOWNLOADS}")
    safe_print(f"Unique URLs inspected across hashtags: {len(seen_urls)}")

    if downloaded_count < REQUIRED_DOWNLOADS:
        safe_print("Not enough videos passed the filters.")
        safe_print("Try lowering MIN_VIEWS, increasing MAX_DURATION_SECONDS, or adding more hashtags.")


if __name__ == "__main__":
    main()