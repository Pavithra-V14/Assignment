"""
Google Drive folder sync.

This is the seam that replaces "someone manually drops an .xlsx into
sample_data/" with "the PM edits the workbook that already lives in a
shared Drive folder, and the next scheduled run just sees the update."

Design:
  - Service-account auth, not OAuth-user auth. A service account has no
    interactive login/consent screen and its key never expires, which is
    what an unattended weekly cron/Action needs. The folder is shared with
    the service account's email address once, like sharing it with a
    teammate (see docs/GOOGLE_DRIVE_SETUP.md).
  - Only the folder is trusted, not a hardcoded file list: every .xlsx (and
    every native Google Sheet, auto-exported to .xlsx) directly inside the
    folder is synced. Add/remove/rename files in the Drive folder and nothing
    on this side needs to change.
  - Incremental: a manifest (`.manifest.json`) in the cache dir records each
    file's Drive `modifiedTime`. Unchanged files are not re-downloaded, so a
    folder with 200 project plans doesn't mean 200 downloads every run.
  - Deletions in Drive are mirrored: a cached file whose Drive source
    disappeared is removed locally, so stale project plans don't keep
    getting scored forever.
  - Transient Drive/API errors (rate limits, 5xx) are retried with backoff;
    anything else (bad credentials, folder not shared) fails fast with a
    typed exception rather than retrying forever.
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from project_health_agent.core.config import settings
from project_health_agent.core.exceptions import DriveAuthError, DriveSyncError
from project_health_agent.core.logging_config import get_logger

logger = get_logger("ingestion.drive_client")

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_GSHEET_MIME = "application/vnd.google-apps.spreadsheet"
_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

_FOLDER_ID_PATTERNS = [
    r"/folders/([a-zA-Z0-9_-]+)",   # https://drive.google.com/drive/folders/<id>
    r"[?&]id=([a-zA-Z0-9_-]+)",     # https://drive.google.com/open?id=<id>
]


def parse_folder_id(folder_url_or_id: str) -> str:
    """Accepts a pasted Drive folder link OR a bare folder ID; returns the ID."""
    value = folder_url_or_id.strip()
    if "/" not in value and "?" not in value:
        return value  # already looks like a bare ID
    for pattern in _FOLDER_ID_PATTERNS:
        match = re.search(pattern, value)
        if match:
            return match.group(1)
    raise DriveSyncError(
        f"Could not extract a folder ID from '{folder_url_or_id}'. Expected a "
        "link like https://drive.google.com/drive/folders/<ID>, or a bare ID."
    )


def _load_credentials():
    # Imported lazily so `pip install` without the `drive` extra still lets
    # the rest of the package (local-file mode) import and run cleanly.
    try:
        from google.oauth2 import service_account
    except ImportError as exc:  # pragma: no cover
        raise DriveAuthError(
            "google-auth / google-api-python-client are not installed. "
            "Install with: pip install 'project-health-agent[drive]'"
        ) from exc

    if settings.GOOGLE_SERVICE_ACCOUNT_FILE:
        try:
            return service_account.Credentials.from_service_account_file(
                settings.GOOGLE_SERVICE_ACCOUNT_FILE, scopes=_SCOPES
            )
        except (OSError, ValueError) as exc:
            raise DriveAuthError(
                f"Could not read service account file "
                f"'{settings.GOOGLE_SERVICE_ACCOUNT_FILE}': {exc}"
            ) from exc

    if settings.GOOGLE_SERVICE_ACCOUNT_JSON_BASE64:
        try:
            raw = base64.b64decode(settings.GOOGLE_SERVICE_ACCOUNT_JSON_BASE64)
            info = json.loads(raw)
            return service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)
        except (ValueError, json.JSONDecodeError) as exc:
            raise DriveAuthError(
                "GOOGLE_SERVICE_ACCOUNT_JSON_BASE64 is set but is not valid "
                "base64-encoded service-account JSON."
            ) from exc

    raise DriveAuthError(
        "DATA_SOURCE=drive requires either GOOGLE_SERVICE_ACCOUNT_FILE or "
        "GOOGLE_SERVICE_ACCOUNT_JSON_BASE64 to be set. See "
        "docs/GOOGLE_DRIVE_SETUP.md."
    )


def _build_service():
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover
        raise DriveAuthError(
            "google-api-python-client is not installed. "
            "Install with: pip install 'project-health-agent[drive]'"
        ) from exc

    creds = _load_credentials()
    return build("drive", "v3", credentials=creds, cache_discovery=False)


_RETRYABLE = (ConnectionError, TimeoutError)


@retry(
    reraise=True,
    stop=stop_after_attempt(settings.DRIVE_MAX_RETRIES),
    wait=wait_exponential(multiplier=1, min=1, max=20),
    retry=retry_if_exception_type(_RETRYABLE),
)
def _list_folder_files(service, folder_id: str) -> list[dict]:
    query = (
        f"'{folder_id}' in parents and trashed = false and "
        f"(mimeType = '{_XLSX_MIME}' or mimeType = '{_GSHEET_MIME}')"
    )
    files: list[dict] = []
    page_token = None
    while True:
        try:
            response = (
                service.files()
                .list(
                    q=query,
                    fields="nextPageToken, files(id, name, mimeType, modifiedTime)",
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
        except Exception as exc:  # googleapiclient.errors.HttpError, socket errors, etc.
            raise DriveSyncError(f"Failed to list Drive folder '{folder_id}': {exc}") from exc
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return files


@retry(
    reraise=True,
    stop=stop_after_attempt(settings.DRIVE_MAX_RETRIES),
    wait=wait_exponential(multiplier=1, min=1, max=20),
    retry=retry_if_exception_type(_RETRYABLE),
)
def _download_file(service, file_meta: dict, dest_path: Path) -> None:
    import googleapiclient.http

    if file_meta["mimeType"] == _GSHEET_MIME:
        request = service.files().export_media(
            fileId=file_meta["id"],
            mimeType=_XLSX_MIME,
        )
    else:
        request = service.files().get_media(fileId=file_meta["id"], supportsAllDrives=True)

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")
    try:
        with open(tmp_path, "wb") as fh:
            downloader = googleapiclient.http.MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk(num_retries=settings.DRIVE_MAX_RETRIES)
        tmp_path.replace(dest_path)
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        raise DriveSyncError(f"Failed to download '{file_meta['name']}': {exc}") from exc


def _load_manifest(cache_dir: Path) -> dict:
    manifest_path = cache_dir / ".manifest.json"
    if manifest_path.exists():
        try:
            return json.loads(manifest_path.read_text())
        except json.JSONDecodeError:
            logger.warning("Drive cache manifest was corrupt; rebuilding from scratch.")
    return {}


def _save_manifest(cache_dir: Path, manifest: dict) -> None:
    (cache_dir / ".manifest.json").write_text(json.dumps(manifest, indent=2))


def sync_folder(folder_url_or_id: str, cache_dir: str | Path) -> list[Path]:
    """
    Sync every project-plan workbook in a Drive folder to a local cache dir.

    Returns the list of local file paths current as of this run (same
    contract as `glob.glob(local_dir/*.xlsx)`, so it's a drop-in swap for the
    local-file path in ingestion/source.py). Safe to call every run: only
    changed files are re-downloaded, and files removed from Drive are
    removed from the cache.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    folder_id = parse_folder_id(folder_url_or_id)

    logger.info("Connecting to Google Drive (folder id: %s)", folder_id)
    service = _build_service()
    remote_files = _list_folder_files(service, folder_id)

    if not remote_files:
        logger.warning(
            "Drive folder %s contains no .xlsx / Google Sheets files, or the "
            "service account does not have access to it.",
            folder_id,
        )

    manifest = _load_manifest(cache_dir)
    new_manifest: dict = {}
    local_paths: list[Path] = []

    for meta in remote_files:
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", meta["name"])
        if not safe_name.lower().endswith(".xlsx"):
            safe_name += ".xlsx"
        dest_path = cache_dir / safe_name

        cached = manifest.get(meta["id"])
        if cached and cached.get("modifiedTime") == meta["modifiedTime"] and dest_path.exists():
            logger.debug("Unchanged, skipping download: %s", meta["name"])
        else:
            logger.info("Downloading (new/changed): %s", meta["name"])
            _download_file(service, meta, dest_path)

        new_manifest[meta["id"]] = {
            "name": meta["name"],
            "modifiedTime": meta["modifiedTime"],
            "local_path": str(dest_path),
        }
        local_paths.append(dest_path)

    # Mirror deletions: drop cached files whose Drive source is gone.
    stale_ids = set(manifest) - set(new_manifest)
    for stale_id in stale_ids:
        stale_path = Path(manifest[stale_id]["local_path"])
        logger.info("Removing stale cached file (deleted from Drive): %s", stale_path.name)
        stale_path.unlink(missing_ok=True)

    _save_manifest(cache_dir, new_manifest)
    logger.info("Drive sync complete: %d file(s) current.", len(local_paths))
    return local_paths
