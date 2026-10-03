"""Thin Drive helpers for the pilot. Read-only in this mode."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build


DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"

GOOGLE_NATIVE_MIMES = {
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.presentation",
    "application/vnd.google-apps.form",
    "application/vnd.google-apps.drawing",
    "application/vnd.google-apps.script",
    "application/vnd.google-apps.map",
    "application/vnd.google-apps.site",
}


# Drive mimeType -> canonical file extension. Used when the filename itself
# has no (or an unrecognized) extension — common when a creator uploads a
# renamed MOV from iOS Files.
MIME_TO_EXTENSION: Dict[str, str] = {
    "video/quicktime":               ".mov",
    "video/mp4":                     ".mp4",
    "video/x-m4v":                   ".mp4",
    "video/mpeg":                    ".mp4",
    "image/jpeg":                    ".jpg",
    "image/pjpeg":                   ".jpg",
    "image/png":                     ".png",
    "image/heic":                    ".heic",
    "image/heif":                    ".heic",
}


def resolve_extension(filename: str, mime_type: Optional[str]) -> str:
    """Return the extension we should treat this file as, lowercase incl. dot.

    Preference order:
      1. Filename's actual extension if it's in our supported set.
      2. MIME-type fallback if the filename has no (or an unrecognized) ext.
      3. Filename's actual extension (may itself be empty/unsupported —
         caller flags Unsupported downstream).
    """
    from . import vocabulary as v
    from pathlib import Path as _P

    ext = _P(filename).suffix.lower()
    if ext and ext in v.SUPPORTED_EXTENSIONS:
        return ext
    inferred = MIME_TO_EXTENSION.get(mime_type or "")
    if inferred and inferred in v.SUPPORTED_EXTENSIONS:
        return inferred
    return ext


def build_drive_service(service_account_path: str):
    creds = service_account.Credentials.from_service_account_file(
        service_account_path, scopes=DRIVE_SCOPES
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------------
# Folder discovery
# ---------------------------------------------------------------------------

def list_shared_root_folders(drive) -> List[Dict[str, Any]]:
    """Return every folder the service account can see that is a top-level
    entry point — i.e. its parent is not itself visible to the SA.

    In practice this yields the folders the client shared directly with the SA,
    not the child folders inside those (those are picked up by recursion).
    """
    q = f"trashed=false and mimeType='{FOLDER_MIME}'"
    all_folders: List[Dict[str, Any]] = []
    page = None
    while True:
        resp = drive.files().list(
            q=q, pageSize=1000, pageToken=page,
            fields="nextPageToken, files(id,name,parents,owners(emailAddress),driveId,shared)",
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        all_folders.extend(resp.get("files", []))
        page = resp.get("nextPageToken")
        if not page:
            break

    visible_ids = {f["id"] for f in all_folders}
    roots: List[Dict[str, Any]] = []
    for f in all_folders:
        parents = f.get("parents") or []
        # A folder is a "root" for our purposes if none of its parents are
        # themselves visible to us. Folders in a Shared Drive we're a member
        # of will have a driveId but no visible parent inside our scope.
        if not parents or not any(pid in visible_ids for pid in parents):
            roots.append(f)
    return roots


# ---------------------------------------------------------------------------
# Media listing
# ---------------------------------------------------------------------------

def _list_children(drive, folder_id: str) -> List[Dict[str, Any]]:
    """Direct children (files + folders) of folder_id."""
    q = f"'{folder_id}' in parents and trashed=false"
    out: List[Dict[str, Any]] = []
    page = None
    while True:
        resp = drive.files().list(
            q=q, pageSize=1000, pageToken=page,
            fields=(
                "nextPageToken, files(id,name,mimeType,size,md5Checksum,"
                "modifiedTime,videoMediaMetadata,imageMediaMetadata)"
            ),
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        out.extend(resp.get("files", []))
        page = resp.get("nextPageToken")
        if not page:
            break
    return out


def list_folder_media(
    drive,
    folder_id: str,
    *,
    recursive: bool = False,
    _path_prefix: str = "",
) -> List[Dict[str, Any]]:
    """List downloadable media inside folder_id.

    Skips Google-native docs (Sheets/Docs/Slides/etc.) — they are not creator
    content and can only be exported, not binary-downloaded.

    When ``recursive`` is True, walks the whole subtree and annotates each
    returned dict with:
      - ``folder_path``: '/'-joined path from folder_id (empty for the root)
      - ``parent_folder_id``: the ID of the file's direct parent folder
    """
    out: List[Dict[str, Any]] = []
    for child in _list_children(drive, folder_id):
        mt = child.get("mimeType")
        if mt == FOLDER_MIME:
            if recursive:
                sub_prefix = f"{_path_prefix}/{child['name']}" if _path_prefix else child["name"]
                out.extend(
                    list_folder_media(
                        drive, child["id"], recursive=True, _path_prefix=sub_prefix
                    )
                )
            continue
        if mt in GOOGLE_NATIVE_MIMES:
            continue
        child["folder_path"] = _path_prefix
        child["parent_folder_id"] = folder_id
        out.append(child)
    return out


def get_folder_meta(drive, folder_id: str) -> Dict[str, Any]:
    """Fetch a folder's own metadata (name, parents, driveId)."""
    return drive.files().get(
        fileId=folder_id,
        fields="id,name,parents,driveId,mimeType,owners(emailAddress)",
        supportsAllDrives=True,
    ).execute()
