"""Source discovery layer — normalizes Drive and local inputs into a common
shape the orchestrator can consume without knowing where the file came from.

Every discoverer returns a list of dicts with these keys:

    id                  str   — drive_file_id OR sha1(abs_local_path)[:16]
    name                str   — display name (with extension when known)
    size                int   — bytes
    mimeType            str|None
    folder_path         str   — relative to the source root; "" for singles
    _root_id            str   — source-root ID (drive folder ID or local dir)
    _root_name          str   — human name of the source root
    source_type         str   — "drive_folder" | "drive_file" | "local_dir" | "local_file"
    local_path          str|None — if pre-supplied on disk (local sources)
    md5Checksum         str|None — Drive-provided if available
    videoMediaMetadata  dict|None — Drive-provided if available

Keeping the dict shape (instead of a dataclass) minimizes churn in
``run_pilot.py`` which was already dict-shaped for Drive listings.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import vocabulary as v
from .drive_client import (
    get_folder_meta,
    list_folder_media,
    list_shared_root_folders,
    resolve_extension,
)


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------

def _local_id(path: str) -> str:
    """Stable 16-char ID for a local file path (used as log filename)."""
    return hashlib.sha1(os.path.abspath(path).encode("utf-8")).hexdigest()[:16]


def _local_mime(name: str) -> Optional[str]:
    """Best-effort MIME guess for a local filename."""
    guess, _ = mimetypes.guess_type(name)
    return guess


def _is_supported_local(name: str, mime: Optional[str]) -> bool:
    """A local file counts as supported if its extension is in our supported
    set, or its MIME type resolves to one we can classify (video/image)."""
    ext = resolve_extension(name, mime)
    return ext in v.SUPPORTED_EXTENSIONS


# ---------------------------------------------------------------------------
# Drive sources
# ---------------------------------------------------------------------------

def discover_drive_folder(
    drive,
    folder_id: str,
    *,
    recursive: bool = True,
) -> List[Dict[str, Any]]:
    """List all media in a Drive folder (recursively by default). Returns
    dicts in the common shape."""
    meta = get_folder_meta(drive, folder_id)
    root_name = meta.get("name", folder_id)
    files = list_folder_media(drive, folder_id, recursive=recursive)
    out: List[Dict[str, Any]] = []
    for f in files:
        out.append({
            "id":                  f["id"],
            "name":                f["name"],
            "size":                int(f.get("size") or 0),
            "mimeType":            f.get("mimeType"),
            "folder_path":         f.get("folder_path", ""),
            "_root_id":            folder_id,
            "_root_name":          root_name,
            "source_type":         "drive_folder",
            "local_path":          None,
            "md5Checksum":         f.get("md5Checksum"),
            "videoMediaMetadata":  f.get("videoMediaMetadata"),
        })
    return out


def discover_drive_folders_all(
    drive,
    *,
    recursive: bool = True,
) -> List[Dict[str, Any]]:
    """Every folder shared with the SA, flattened. Same as the pre-existing
    auto-discover mode."""
    out: List[Dict[str, Any]] = []
    for root in list_shared_root_folders(drive):
        out.extend(discover_drive_folder(drive, root["id"], recursive=recursive))
    return out


def discover_drive_file(drive, file_id: str) -> List[Dict[str, Any]]:
    """Single Drive file. Returns a one-element list for shape parity."""
    meta = drive.files().get(
        fileId=file_id,
        fields=("id,name,mimeType,size,md5Checksum,parents,"
                "videoMediaMetadata,imageMediaMetadata"),
        supportsAllDrives=True,
    ).execute()
    return [{
        "id":                  meta["id"],
        "name":                meta["name"],
        "size":                int(meta.get("size") or 0),
        "mimeType":            meta.get("mimeType"),
        "folder_path":         "",
        "_root_id":            meta["id"],
        "_root_name":          meta["name"],
        "source_type":         "drive_file",
        "local_path":          None,
        "md5Checksum":         meta.get("md5Checksum"),
        "videoMediaMetadata":  meta.get("videoMediaMetadata"),
    }]


# ---------------------------------------------------------------------------
# Local sources
# ---------------------------------------------------------------------------

def discover_local_dir(
    path: str,
    *,
    recursive: bool = True,
) -> List[Dict[str, Any]]:
    """Walk a local directory and return every supported media file."""
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Local directory not found: {root}")

    iterator = root.rglob("*") if recursive else root.iterdir()
    out: List[Dict[str, Any]] = []
    for p in iterator:
        if not p.is_file(): continue
        if p.name.startswith("."): continue  # skip .DS_Store etc.
        mime = _local_mime(p.name)
        if not _is_supported_local(p.name, mime):
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        rel_parent = str(p.parent.relative_to(root))
        rel_parent = "" if rel_parent == "." else rel_parent
        out.append({
            "id":                  _local_id(str(p)),
            "name":                p.name,
            "size":                size,
            "mimeType":            mime,
            "folder_path":         rel_parent,
            "_root_id":            str(root),
            "_root_name":          root.name or str(root),
            "source_type":         "local_dir",
            "local_path":          str(p),
            "md5Checksum":         None,
            "videoMediaMetadata":  None,
        })
    out.sort(key=lambda f: (f["folder_path"], f["name"]))
    return out


def discover_local_file(path: str) -> List[Dict[str, Any]]:
    """Single local file. Returns a one-element list for shape parity."""
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"Local file not found: {p}")
    mime = _local_mime(p.name)
    if not _is_supported_local(p.name, mime):
        raise ValueError(
            f"Unsupported file type for {p.name} (mime={mime}). "
            f"Supported extensions: {sorted(v.SUPPORTED_EXTENSIONS)}"
        )
    return [{
        "id":                  _local_id(str(p)),
        "name":                p.name,
        "size":                p.stat().st_size,
        "mimeType":            mime,
        "folder_path":         "",
        "_root_id":            str(p.parent),
        "_root_name":          p.parent.name or str(p.parent),
        "source_type":         "local_file",
        "local_path":          str(p),
        "md5Checksum":         None,
        "videoMediaMetadata":  None,
    }]
