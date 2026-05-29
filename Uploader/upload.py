"""
Bulk-upload downloaded TikTok videos into Loorio.

Reads the manifests written by ../Downloader/downloader.py (metadata.json) and,
for each video, runs Loorio's three-step upload flow:

    1. POST /videos/upload/init      -> { uploadUrl, s3Key, publicUrl }
    2. PUT  <uploadUrl>  (raw bytes) -> stores the file in object storage
    3. POST /videos/upload/complete  -> { id }   (the new video)

Auth is a bearer token from POST /auth/login. A profile must exist, so we
ensure one before uploading.

The downloader writes a two-level layout, one folder per topic:

    downloads/<category-slug>/<topic-slug>/metadata.json

Each record stores 'topic_slug' and 'category_slug'. With --all this script
walks that tree, auto-resolves the topic per record (no need to pass --topic
once per folder), and falls back to the folder name if a record lacks the slug.
Old single-folder manifests still work via --path <folder> without --all.

Local dev usage:

    # Upload one topic folder:
    python upload.py --path ../Downloader/downloads/mathematics/calculus \
        --email usera@loorio.test --password password123

    # Walk the whole taxonomy tree, each video routed to its own topic:
    python upload.py --path ../Downloader/downloads --all \
        --email usera@loorio.test --password password123

    # Force a single topic onto everything (legacy override):
    python upload.py --path ../Downloader/downloads/Finance \
        --topic general --category finance-economics \
        --email finance@loorio.com --password 'finance@loorio'
"""

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests


# ==========================
# SETTINGS
# ==========================

DEFAULT_BASE_URL = "http://localhost:3000"
METADATA_FILENAME = "metadata.json"
LEDGER_FILENAME = "uploaded.json"

DEFAULT_MIME_TYPE = "video/mp4"
API_TIMEOUT_SECONDS = 60
UPLOAD_TIMEOUT_SECONDS = 600


def log(text: str):
    print(text, flush=True)


# ==========================
# HTTP HELPERS
# ==========================

def post_json(session: requests.Session, url: str, payload: dict, token: Optional[str] = None) -> requests.Response:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    return session.post(url, json=payload, headers=headers, timeout=API_TIMEOUT_SECONDS)


def expect_ok(response: requests.Response, action: str) -> dict:
    """Returns parsed JSON or raises with the server's error body attached."""

    if not response.ok:
        raise RuntimeError(
            f"{action} failed: HTTP {response.status_code} -> {response.text[:500]}"
        )

    try:
        return response.json()
    except ValueError:
        return {}


# ==========================
# AUTH + PROFILE
# ==========================

def register(session: requests.Session, base: str, email: str, password: str):
    """Best-effort registration. A 400 (already exists) is fine."""

    response = post_json(session, f"{base}/auth/register", {"email": email, "password": password})

    if response.status_code == 400:
        log("Account already exists, continuing to login.")
        return

    expect_ok(response, "Register")
    log(f"Registered {email}.")


def login(session: requests.Session, base: str, email: str, password: str) -> str:
    response = post_json(session, f"{base}/auth/login", {"email": email, "password": password})
    data = expect_ok(response, "Login")

    token = data.get("token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("Login succeeded but no token was returned.")

    log(f"Logged in as {email}.")
    return token


def ensure_profile(session: requests.Session, base: str, token: str, username: str, bio: str):
    """Creates a profile only if the account doesn't already have one."""

    me = session.get(
        f"{base}/users/me",
        headers={"Authorization": f"Bearer {token}"},
        timeout=API_TIMEOUT_SECONDS,
    )

    if me.ok:
        existing = me.json().get("username", "?")
        log(f"Profile already exists (@{existing}).")
        return

    if me.status_code != 404:
        raise RuntimeError(f"Could not check profile: HTTP {me.status_code} -> {me.text[:300]}")

    response = post_json(session, f"{base}/profile/create", {"username": username, "bio": bio}, token=token)
    expect_ok(response, "Create profile")
    log(f"Created profile @{username}.")


# ==========================
# TOPIC RESOLUTION
# ==========================

def fetch_categories(session: requests.Session, base: str, token: str) -> list:
    response = session.get(
        f"{base}/categories",
        headers={"Authorization": f"Bearer {token}"},
        timeout=API_TIMEOUT_SECONDS,
    )
    data = expect_ok(response, "Fetch categories")
    return data if isinstance(data, list) else []


def build_topic_index(session: requests.Session, base: str, token: str) -> dict:
    """
    Fetches /categories once and indexes topics by both slug and name (lowercased)
    so per-record resolution is just a dict lookup.

        index[key] -> list of { category_slug, category_name, topic_slug,
                                topic_name, topic_id }
    """

    index: dict = {}
    for category in fetch_categories(session, base, token):
        category_slug = str(category.get("slug") or "").lower()
        category_name = str(category.get("name") or "")

        for topic in category.get("topics", []):
            entry = {
                "category_slug": category_slug,
                "category_name": category_name,
                "topic_slug": str(topic.get("slug") or "").lower(),
                "topic_name": str(topic.get("name") or ""),
                "topic_id": topic["id"],
            }
            for key in {entry["topic_slug"], entry["topic_name"].lower()}:
                if key:
                    index.setdefault(key, []).append(entry)

    return index


def resolve_via_index(index: dict, topic_hint: str, category_hint: Optional[str]) -> str:
    """Returns the topic_id matching topic_hint, narrowed by category_hint if given."""

    needle = (topic_hint or "").lower()
    if not needle or needle not in index:
        raise RuntimeError(f"Topic '{topic_hint}' not found in Loorio's taxonomy.")

    candidates = index[needle]

    if category_hint:
        cat = category_hint.lower()
        candidates = [
            e for e in candidates
            if cat in {e["category_slug"], e["category_name"].lower()}
        ]
        if not candidates:
            raise RuntimeError(f"Topic '{topic_hint}' not found inside category '{category_hint}'.")

    if len(candidates) > 1:
        where = ", ".join(f"{e['category_name']}/{e['topic_name']}" for e in candidates)
        raise RuntimeError(
            f"Topic '{topic_hint}' is ambiguous ({where}). "
            "Pass --category or set 'category_slug' in the manifest record."
        )

    return candidates[0]["topic_id"]


def topic_hints_from_record(record: dict, directory: Path) -> tuple:
    """
    Returns (topic_hint, category_hint) for resolution. Prefers the record's
    explicit slugs; falls back to the folder names from the layout
    <...>/<category-slug>/<topic-slug>/.
    """

    topic_hint = record.get("topic_slug") or record.get("topic_name") or directory.name
    category_hint = (
        record.get("category_slug")
        or record.get("category_name")
        or record.get("category")  # legacy field
        or directory.parent.name
    )
    return topic_hint, category_hint


def build_caption(record: dict, fallback: str) -> str:
    """Joins title + description like the mobile app; falls back to caption/filename."""

    title = (record.get("title") or "").strip()
    description = (record.get("description") or "").strip()

    if description:
        return f"{title}\n{description}".strip()
    if title:
        return title
    return (record.get("caption") or fallback).strip()


# ==========================
# MANIFEST + LEDGER
# ==========================

def load_records(manifest_path: Path) -> list:
    if not manifest_path.exists():
        return []

    try:
        records = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise RuntimeError(f"Cannot read manifest {manifest_path}: {error}")

    if not isinstance(records, list):
        raise RuntimeError(f"Manifest {manifest_path} is not a JSON array.")

    return records


def load_ledger(ledger_path: Path) -> dict:
    if not ledger_path.exists():
        return {}

    try:
        return json.loads(ledger_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_ledger(ledger_path: Path, ledger: dict):
    ledger_path.write_text(json.dumps(ledger, indent=2, ensure_ascii=False), encoding="utf-8")


def find_manifest_dirs(root: Path, walk_all: bool) -> list:
    """
    Returns directories containing a metadata.json to process.

    --all walks the whole tree under root (handles the new
    <category>/<topic>/ layout and any future deeper nesting). Without --all,
    root itself must contain metadata.json.
    """

    if not walk_all:
        if (root / METADATA_FILENAME).exists():
            return [root]
        log(f"No {METADATA_FILENAME} at {root}. Did you mean to pass --all?")
        return []

    dirs = sorted({manifest.parent for manifest in root.rglob(METADATA_FILENAME)})
    if not dirs:
        log(f"No {METADATA_FILENAME} files found anywhere under {root}.")
    return dirs


# ==========================
# DURATION FALLBACK
# ==========================

def probe_duration_ms(video_path: Path) -> Optional[int]:
    """Uses ffprobe to recover duration when the manifest lacks it."""

    if shutil.which("ffprobe") is None:
        return None

    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        return int(float(result.stdout.strip()) * 1000)
    except (ValueError, subprocess.SubprocessError):
        return None


# ==========================
# UPLOAD FLOW (per video)
# ==========================

def upload_video(
    session: requests.Session,
    base: str,
    token: str,
    video_path: Path,
    record: dict,
    topic_id: Optional[str],
    learning_mode: bool,
) -> str:
    file_size = video_path.stat().st_size
    mime_type = record.get("mimeType") or DEFAULT_MIME_TYPE
    caption = build_caption(record, video_path.stem)

    # Loorio's DTO requires durationMs >= 1 when present, so omit it if unknown.
    duration_ms = record.get("durationMs")
    if not isinstance(duration_ms, int) or duration_ms < 1:
        probed = probe_duration_ms(video_path)
        duration_ms = probed if isinstance(probed, int) and probed >= 1 else None

    # Step 1: init
    init_payload = {
        "fileName": video_path.name,
        "mimeType": mime_type,
        "fileSize": file_size,
    }
    if duration_ms:
        init_payload["durationMs"] = duration_ms

    init = expect_ok(
        post_json(session, f"{base}/videos/upload/init", init_payload, token=token),
        "Upload init",
    )
    upload_url = init.get("uploadUrl")
    raw_s3_key = init.get("rawS3Key")
    canonical_s3_key = init.get("canonicalS3Key")
    canonical_public_url = init.get("canonicalPublicUrl")
    if not upload_url or not raw_s3_key or not canonical_s3_key or not canonical_public_url:
        raise RuntimeError(f"Upload init response missing required fields. Got: {init}")

    # Step 2: PUT the raw bytes to the presigned URL (no auth header here).
    with video_path.open("rb") as handle:
        put = session.put(
            upload_url,
            data=handle,
            headers={"Content-Type": mime_type},
            timeout=UPLOAD_TIMEOUT_SECONDS,
        )
    if not put.ok:
        raise RuntimeError(f"Storage PUT failed: HTTP {put.status_code} -> {put.text[:300]}")

    # Step 3: complete — full metadata, mirroring the mobile app's payload.
    complete_payload = {
        "rawS3Key": raw_s3_key,
        "canonicalS3Key": canonical_s3_key,
        "canonicalPublicUrl": canonical_public_url,
        "sizeBytes": file_size,
        "mimeType": mime_type,
        "learningMode": learning_mode,
    }
    if caption:
        complete_payload["caption"] = caption[:1000]
    if duration_ms:
        complete_payload["durationMs"] = duration_ms
    if topic_id:
        complete_payload["topicId"] = topic_id
    thumbnail_url = record.get("thumbnail_url")
    if thumbnail_url:
        complete_payload["thumbnailUrl"] = thumbnail_url

    complete = expect_ok(
        post_json(session, f"{base}/videos/upload/complete", complete_payload, token=token),
        "Upload complete",
    )

    video_id = complete.get("id")
    if not video_id:
        raise RuntimeError("Upload completed but no video id was returned.")

    return video_id


def process_directory(
    session: requests.Session,
    base: str,
    token: str,
    directory: Path,
    args,
    override_topic_id: Optional[str],
    topic_index: Optional[dict],
) -> tuple:
    """
    Uploads every video listed in directory/metadata.json.

    Topic resolution per record:
      1. override_topic_id (from --topic / --topic-id) wins for the whole run.
      2. Otherwise, the record's topic_slug/category_slug (or folder name
         fallback) is looked up in topic_index.
      3. If neither route yields an id, the record is skipped with a warning.

    Returns (ok, skipped, failed).
    """

    records = load_records(directory / METADATA_FILENAME)
    if not records:
        log(f"No records in {directory / METADATA_FILENAME}, skipping.")
        return (0, 0, 0)

    ledger_path = directory / LEDGER_FILENAME
    ledger = load_ledger(ledger_path)

    log(f"\n{'=' * 60}\nFOLDER: {directory}  ({len(records)} records)\n{'=' * 60}")

    ok = skipped = failed = 0

    for index, record in enumerate(records, start=1):
        if args.limit and ok >= args.limit:
            break

        file_name = record.get("file")
        video_id = record.get("video_id") or file_name
        if not file_name:
            log(f"[{index}] No 'file' field, skipping record.")
            skipped += 1
            continue

        if video_id in ledger:
            log(f"[{index}] Already uploaded ({file_name}), skipping.")
            skipped += 1
            continue

        video_path = directory / file_name
        if not video_path.exists():
            log(f"[{index}] Missing file on disk: {video_path}, skipping.")
            skipped += 1
            continue

        # Resolve the topic for THIS record (cheap dict lookup once index is built).
        record_topic_id = override_topic_id
        if not record_topic_id and topic_index is not None:
            topic_hint, category_hint = topic_hints_from_record(record, directory)
            try:
                record_topic_id = resolve_via_index(topic_index, topic_hint, category_hint)
            except RuntimeError as error:
                log(f"[{index}] Topic resolution failed for {file_name}: {error}")
                skipped += 1
                continue

        log(f"[{index}/{len(records)}] Uploading {file_name} (topic={record_topic_id or '-'}) ...")

        if args.dry_run:
            caption_preview = build_caption(record, video_path.stem).replace("\n", " ")[:60]
            log(f"    (dry-run) caption='{caption_preview}' learningMode={args.learning_mode}")
            ok += 1
            continue

        try:
            new_id = upload_video(session, base, token, video_path, record, record_topic_id, args.learning_mode)
        except (RuntimeError, requests.RequestException) as error:
            log(f"    FAILED: {error}")
            failed += 1
            continue

        ledger[video_id] = {
            "videoId": new_id,
            "topicId": record_topic_id,
            "uploadedAt": datetime.now(timezone.utc).isoformat(),
        }
        save_ledger(ledger_path, ledger)
        log(f"    OK -> Loorio video {new_id}")
        ok += 1

    return (ok, skipped, failed)


# ==========================
# CLI
# ==========================

def parse_args(argv) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bulk-upload downloaded videos into Loorio.")
    parser.add_argument("--path", required=True, help="Folder with videos + metadata.json (or parent with --all).")
    parser.add_argument("--email", required=True, help="Account email.")
    parser.add_argument("--password", required=True, help="Account password.")
    parser.add_argument("--base", default=DEFAULT_BASE_URL, help=f"API base URL (default {DEFAULT_BASE_URL}).")
    parser.add_argument("--username", help="Profile username if one must be created (default: email local part).")
    parser.add_argument("--bio", default="", help="Profile bio used only when creating a profile.")
    parser.add_argument("--register", action="store_true", help="Register the account first (ignores 'already exists').")
    parser.add_argument("--topic", help="Override: force every video in this run onto this topic (name or slug).")
    parser.add_argument("--topic-id", help="Override: topic UUID, used directly (takes precedence over --topic).")
    parser.add_argument("--category", help="Category name/slug to disambiguate --topic when needed.")
    parser.add_argument(
        "--learning-mode",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Set learningMode on each video (default: on). Use --no-learning-mode to disable.",
    )
    parser.add_argument("--all", action="store_true", help="Walk --path recursively for every metadata.json (handles the <category>/<topic>/ layout).")
    parser.add_argument("--limit", type=int, default=0, help="Max uploads per folder (0 = no limit).")
    parser.add_argument("--dry-run", action="store_true", help="List what would upload without calling the API.")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    root = Path(args.path).expanduser().resolve()
    if not root.is_dir():
        log(f"Path is not a directory: {root}")
        return 1

    username = args.username or args.email.split("@", 1)[0]
    base = args.base.rstrip("/")

    session = requests.Session()

    override_topic_id: Optional[str] = args.topic_id
    topic_index: Optional[dict] = None
    token = "DRY-RUN"

    if not args.dry_run:
        try:
            if args.register:
                register(session, base, args.email, args.password)
            token = login(session, base, args.email, args.password)
            ensure_profile(session, base, token, username, args.bio)

            # Build the topic index once. Even if the user passed --topic, we
            # still need it to resolve that hint into an id.
            topic_index = build_topic_index(session, base, token)
            log(f"Loaded topic index ({sum(len(v) for v in topic_index.values())} entries).")

            if not override_topic_id and args.topic:
                override_topic_id = resolve_via_index(topic_index, args.topic, args.category)
                log(f"Resolved override topic '{args.topic}' -> {override_topic_id}")
        except (RuntimeError, requests.RequestException) as error:
            log(f"Setup failed: {error}")
            return 1
    else:
        log("Dry run: skipping auth and topic resolution.")

    directories = find_manifest_dirs(root, args.all)
    if not directories:
        return 1

    log(f"Manifest folders to process: {len(directories)}")

    total_ok = total_skipped = total_failed = 0
    for directory in directories:
        ok, skipped, failed = process_directory(session, base, token, directory, args, override_topic_id, topic_index)
        total_ok += ok
        total_skipped += skipped
        total_failed += failed

    log(f"\n{'=' * 60}\nDONE  uploaded={total_ok}  skipped={total_skipped}  failed={total_failed}\n{'=' * 60}")
    return 1 if total_failed else 0


if __name__ == "__main__":
    sys.exit(main())
