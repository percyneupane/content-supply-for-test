"""
Curate TikTok videos into Loorio's category/topic taxonomy.

Discovery + filtering is driven entirely by ../taxonomy.json (the single source
of truth shared with the Loorio seed). For every selected topic the script:

    1. Pools candidate TikTok URLs from each of the topic's hashtags (Pyktok).
    2. Reads each candidate's metadata (yt-dlp) and applies the filters:
       duration, minimum views, and a topic KEYWORD match in title/description.
    3. Downloads the passing videos with yt-dlp into a per-topic folder:
           downloads/<category-slug>/<topic-slug>/
       and appends a record to that folder's metadata.json (the bulk-upload
       source of truth consumed by Uploader/upload.py).

A single global archive (downloads/downloaded.txt) dedupes across every topic
and across re-runs, so coverage accumulates over time and no video is curated
into two topics.

Usage:

    # One topic:
    python downloader.py --topic calculus --per-topic 15

    # A whole category:
    python downloader.py --category finance-economics --per-topic 10

    # The entire taxonomy:
    python downloader.py --all --per-topic 10
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import pyktok as pyk


# ==========================
# SETTINGS / DEFAULTS
# ==========================

SCRIPT_DIR = Path(__file__).parent
DEFAULT_TAXONOMY_PATH = SCRIPT_DIR.parent / "taxonomy.json"
BASE_DOWNLOAD_DIR = SCRIPT_DIR / "downloads"
TEMP_DIR = SCRIPT_DIR / "temp"
DOWNLOAD_ARCHIVE = BASE_DOWNLOAD_DIR / "downloaded.txt"

# Per-topic manifest written next to the downloaded videos.
METADATA_FILENAME = "metadata.json"
UPLOAD_MIME_TYPE = "video/mp4"

DEFAULT_PER_TOPIC = 10
DEFAULT_MAX_DURATION_SECONDS = 180
DEFAULT_MIN_VIEWS = 10_000

# Browser to borrow TikTok cookies from. Must be a browser you are logged into
# TikTok with. Options: "chrome", "firefox", "safari", "edge".
DEFAULT_BROWSER_NAME = "chrome"

SLEEP_BETWEEN_DOWNLOADS = 1
SUBPROCESS_TIMEOUT_SECONDS = 120


def safe_print(text: str):
    print(text, flush=True)


# ==========================
# BASIC HELPERS
# ==========================

def create_folder(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def run_command(command):
    import subprocess

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
# TAXONOMY
# ==========================

def load_taxonomy(path: Path) -> list:
    """Reads taxonomy.json and returns its list of categories."""

    if not path.exists():
        raise RuntimeError(f"Taxonomy file not found: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise RuntimeError(f"Cannot read taxonomy {path}: {error}")

    categories = data.get("categories")
    if not isinstance(categories, list) or not categories:
        raise RuntimeError(f"Taxonomy {path} has no 'categories' array.")

    return categories


def select_targets(categories: list, args) -> list:
    """
    Returns a flat list of (category, topic) pairs to process based on the
    chosen scope (--all / --category / --topic).
    """

    pairs = []
    for category in categories:
        for topic in category.get("topics", []):
            pairs.append((category, topic))

    if args.all:
        return pairs

    selected = pairs
    if args.category:
        needle = args.category.lower()
        selected = [
            (c, t) for (c, t) in selected
            if needle in {str(c.get("name", "")).lower(), str(c.get("slug", "")).lower()}
        ]
        if not selected:
            raise RuntimeError(f"No category matched '{args.category}'.")

    if args.topic:
        needle = args.topic.lower()
        selected = [
            (c, t) for (c, t) in selected
            if needle in {str(t.get("name", "")).lower(), str(t.get("slug", "")).lower()}
        ]
        if not selected:
            raise RuntimeError(f"No topic matched '{args.topic}'.")

    return selected


# ==========================
# PYKTOK DISCOVERY
# ==========================

def discover_tiktok_urls_from_hashtag(hashtag: str, browser: str, cache: dict) -> list:
    """
    Collects TikTok URLs from a hashtag page via Pyktok (~15-30 per page).
    Results are cached per hashtag for the lifetime of the run, since topics
    frequently share hashtags.
    """

    if hashtag in cache:
        safe_print(f"Using cached discovery for #{hashtag} ({len(cache[hashtag])} URLs).")
        return cache[hashtag]

    create_folder(TEMP_DIR)
    metadata_file = TEMP_DIR / f"{hashtag}_metadata.csv"

    safe_print("\n" + "=" * 60)
    safe_print(f"DISCOVERING #{hashtag} (Pyktok)")
    safe_print("=" * 60)

    if metadata_file.exists():
        metadata_file.unlink()

    try:
        pyk.specify_browser(browser)
        pyk.save_tiktok_multi_page(
            hashtag,
            ent_type="hashtag",
            save_video=False,
            metadata_fn=str(metadata_file),
        )
    except Exception as error:
        safe_print(f"Pyktok discovery failed for #{hashtag}: {error}")
        cache[hashtag] = []
        return []

    if not metadata_file.exists():
        safe_print(f"No metadata file created by Pyktok for #{hashtag}.")
        cache[hashtag] = []
        return []

    df = pd.read_csv(metadata_file)
    urls = extract_urls_from_dataframe(df)
    safe_print(f"#{hashtag}: {len(urls)} URLs extracted from {len(df)} rows.")

    cache[hashtag] = urls
    return urls


def extract_urls_from_dataframe(df: pd.DataFrame) -> list:
    """Reconstructs canonical TikTok URLs from Pyktok metadata columns."""

    seen = set()
    urls = []

    if "video_id" in df.columns and "author_username" in df.columns:
        for _, row in df.iterrows():
            video_id = row.get("video_id")
            username = row.get("author_username")
            if pd.isna(video_id) or pd.isna(username):
                continue

            vid = int(video_id) if str(video_id).isdigit() else video_id
            url = f"https://www.tiktok.com/@{username}/video/{vid}"
            if url not in seen:
                seen.add(url)
                urls.append(url)

    # Fallback: scan every string cell for embedded URLs.
    for _, row in df.iterrows():
        for value in row.values:
            if isinstance(value, str) and "tiktok.com" in value and "/video/" in value:
                cleaned = value.strip()
                if cleaned not in seen:
                    seen.add(cleaned)
                    urls.append(cleaned)

    return urls


# ==========================
# YT-DLP METADATA
# ==========================

def get_ytdlp_metadata(url: str) -> Optional[dict]:
    """Reads TikTok metadata via yt-dlp before deciding to download."""

    command = [
        "yt-dlp", url,
        "--dump-json", "--skip-download",
        "--ignore-errors", "--no-warnings",
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


# ==========================
# FILTER
# ==========================

def explain_skip_reason(video: dict, keywords: list, max_duration: int, min_views: int) -> str:
    """Returns 'Passed' or a human-readable skip reason for one candidate."""

    title = (video.get("title") or "").lower()
    description = (video.get("description") or "").lower()
    searchable_text = f"{title} {description}"

    duration = video.get("duration")
    if duration is None:
        return "Skipped: missing duration"
    if duration > max_duration:
        return f"Skipped: too long ({duration}s > {max_duration}s)"

    # TikTok metadata often omits view_count; treat missing as 0.
    views = video.get("view_count")
    effective_views = views if isinstance(views, int) else 0
    if effective_views < min_views:
        return f"Skipped: not enough views ({effective_views} < {min_views})"

    # Topic keyword gate: at least one keyword must appear (word-boundary match).
    for keyword in keywords:
        pattern = re.compile(rf"\b{re.escape(keyword.lower())}\b")
        if pattern.search(searchable_text):
            return "Passed"

    return "Skipped: no topic keyword found in title or description"


# ==========================
# METADATA MANIFEST
# ==========================

def build_metadata_record(url: str, video: dict, category: dict, topic: dict, file_name: str) -> dict:
    """Normalizes yt-dlp metadata into the shape the Loorio uploader needs."""

    duration = video.get("duration")
    duration_ms = int(duration * 1000) if isinstance(duration, (int, float)) else None

    return {
        "video_id": str(video.get("id") or ""),
        "file": file_name,
        "title": (video.get("title") or "").strip(),
        "description": (video.get("description") or "").strip(),
        "durationMs": duration_ms,
        "mimeType": UPLOAD_MIME_TYPE,
        "category_name": category.get("name"),
        "category_slug": category.get("slug"),
        "topic_name": topic.get("name"),
        "topic_slug": topic.get("slug"),
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


def load_manifest(manifest_path: Path) -> list:
    if not manifest_path.exists():
        return []
    try:
        records = json.loads(manifest_path.read_text(encoding="utf-8"))
        return records if isinstance(records, list) else []
    except (json.JSONDecodeError, OSError):
        safe_print(f"Existing manifest unreadable, starting fresh: {manifest_path}")
        return []


def upsert_metadata_record(manifest_path: Path, record: dict):
    """Appends (or replaces by video_id) a record, building a new list."""

    existing = load_manifest(manifest_path)
    video_id = record.get("video_id")
    others = [r for r in existing if r.get("video_id") != video_id]
    updated = others + [record]

    manifest_path.write_text(
        json.dumps(updated, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    safe_print(f"    Manifest updated: {manifest_path} ({len(updated)} records)")


# ==========================
# DOWNLOAD
# ==========================

def download_with_ytdlp(url: str, video: dict, topic_dir: Path) -> str:
    """
    Downloads one video into topic_dir. Returns 'ok', 'archived' (already in
    the global archive, no new file landed here), or 'failed'.
    """

    import subprocess

    create_folder(topic_dir)

    title = video.get("title") or "Untitled"
    video_id = video.get("id") or "unknown"
    safe_title = clean_filename(title)
    expected_file = topic_dir / f"{safe_title}-{video_id}.mp4"

    safe_print(f"    Downloading: {title[:60]}")

    command = [
        "yt-dlp", url,

        # Prefer the best H.264 stream, which reliably carries audio on TikTok.
        # H.265 (bytevc1) streams advertise aac but often download silent.
        "-f", "b[vcodec^=h264]/b",
        "--merge-output-format", "mp4",

        # Global archive dedupes across topics and re-runs.
        "--download-archive", str(DOWNLOAD_ARCHIVE),

        "-o", str(topic_dir / f"{safe_title}-{video_id}.%(ext)s"),
    ]

    result = subprocess.run(command)

    if result.returncode != 0:
        safe_print("    Download result: failed")
        return "failed"

    # returncode 0 with no file means the archive skipped it (already curated
    # elsewhere). Don't record it under this topic.
    if not expected_file.exists():
        safe_print("    Already in archive (curated under another topic), skipping.")
        return "archived"

    return "ok"


# ==========================
# PER-TOPIC PROCESSING
# ==========================

def process_topic(category: dict, topic: dict, args, cache: dict) -> tuple:
    """
    Curates videos for one topic up to the per-topic target.
    Returns (downloaded, skipped, failed).
    """

    topic_dir = BASE_DOWNLOAD_DIR / category["slug"] / topic["slug"]
    manifest_path = topic_dir / METADATA_FILENAME

    existing = load_manifest(manifest_path)
    existing_ids = {r.get("video_id") for r in existing if r.get("video_id")}
    have = sum(1 for r in existing if (topic_dir / (r.get("file") or "")).exists())

    safe_print("\n" + "#" * 60)
    safe_print(f"TOPIC: {category['name']} / {topic['name']}")
    safe_print(f"Have {have}/{args.per_topic}  hashtags={topic.get('hashtags')}")
    safe_print("#" * 60)

    if have >= args.per_topic:
        safe_print("Target already met, skipping topic.")
        return (0, 0, 0)

    downloaded = skipped = failed = 0
    seen_urls = set()

    for hashtag in topic.get("hashtags", []):
        if have >= args.per_topic:
            break

        urls = discover_tiktok_urls_from_hashtag(hashtag, args.browser, cache)

        for url in urls:
            if have >= args.per_topic:
                break
            if url in seen_urls:
                continue
            seen_urls.add(url)

            video = get_ytdlp_metadata(url)
            if video is None:
                skipped += 1
                continue

            if str(video.get("id") or "") in existing_ids:
                continue  # already curated for this topic

            reason = explain_skip_reason(video, topic["keywords"], args.max_duration, args.min_views)
            if reason != "Passed":
                safe_print(f"    {reason}")
                skipped += 1
                continue

            status = download_with_ytdlp(url, video, topic_dir)
            if status == "ok":
                record = build_metadata_record(url, video, category, topic, f"{clean_filename(video.get('title') or 'Untitled')}-{video.get('id') or 'unknown'}.mp4")
                upsert_metadata_record(manifest_path, record)
                existing_ids.add(record["video_id"])
                downloaded += 1
                have += 1
                safe_print(f"    OK -> {have}/{args.per_topic}")
                time.sleep(SLEEP_BETWEEN_DOWNLOADS)
            elif status == "archived":
                skipped += 1
            else:
                failed += 1

    if have < args.per_topic:
        safe_print(f"Only {have}/{args.per_topic} for this topic. Add hashtags or lower --min-views.")

    return (downloaded, skipped, failed)


# ==========================
# CLI
# ==========================

def parse_args(argv) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Curate TikTok videos into the Loorio taxonomy.")

    scope = parser.add_argument_group("scope (choose at least one)")
    scope.add_argument("--all", action="store_true", help="Process every category and topic in the taxonomy.")
    scope.add_argument("--category", help="Category name or slug to process (all its topics).")
    scope.add_argument("--topic", help="Topic name or slug to process.")

    parser.add_argument("--per-topic", type=int, default=DEFAULT_PER_TOPIC, help=f"Target videos per topic (default {DEFAULT_PER_TOPIC}).")
    parser.add_argument("--min-views", type=int, default=DEFAULT_MIN_VIEWS, help=f"Minimum view count (default {DEFAULT_MIN_VIEWS}).")
    parser.add_argument("--max-duration", type=int, default=DEFAULT_MAX_DURATION_SECONDS, help=f"Max duration in seconds (default {DEFAULT_MAX_DURATION_SECONDS}).")
    parser.add_argument("--browser", default=DEFAULT_BROWSER_NAME, help=f"Browser to borrow TikTok cookies from (default {DEFAULT_BROWSER_NAME}).")
    parser.add_argument("--taxonomy", default=str(DEFAULT_TAXONOMY_PATH), help="Path to taxonomy.json.")

    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    if not (args.all or args.category or args.topic):
        safe_print("Choose a scope: --all, --category <slug>, or --topic <slug>.")
        return 1

    try:
        categories = load_taxonomy(Path(args.taxonomy).expanduser().resolve())
        targets = select_targets(categories, args)
    except RuntimeError as error:
        safe_print(str(error))
        return 1

    create_folder(BASE_DOWNLOAD_DIR)

    safe_print("\nTikTok taxonomy curator")
    safe_print(f"Topics selected: {len(targets)}")
    safe_print(f"Per-topic target: {args.per_topic}  min-views: {args.min_views}  max-duration: {args.max_duration}s")

    cache: dict = {}
    total_ok = total_skipped = total_failed = 0

    for category, topic in targets:
        ok, skipped, failed = process_topic(category, topic, args, cache)
        total_ok += ok
        total_skipped += skipped
        total_failed += failed

    safe_print("\n" + "=" * 60)
    safe_print(f"DONE  downloaded={total_ok}  skipped={total_skipped}  failed={total_failed}")
    safe_print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
