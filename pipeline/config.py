"""Central config. Reads from env vars first, then falls back to sensible
project-local defaults so `python scripts/run_pilot.py` just works.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_SA_PATH = str(PROJECT_ROOT / "ps-naming-convention-ba14917d9579.json")
DEFAULT_OUTPUT_DIR = str(PROJECT_ROOT / "pilot_output")
# /tmp is local-only (never touched by macOS iCloud offload). Losing this on
# reboot just triggers re-downloads on the next run.
DEFAULT_MEDIA_CACHE = "/tmp/pawpystuff_media_cache"
DEFAULT_MODEL = "gemini-flash-latest"


@dataclass
class Config:
    # Required for Drive sources; optional when only local sources are used
    # (validated by the caller before Drive discovery, not here).
    service_account_path: str | None
    gemini_api_key: str
    # None -> auto-discover every folder shared with the SA (Drive-folder mode
    # only). Set PS_FOLDER_ID=... in the environment to restrict to one folder.
    pilot_folder_id: str | None
    output_dir: str
    media_cache_dir: str
    model: str
    max_files: int | None = None      # limit for smoke tests
    frame_max_edge: int = 1024        # downscale target for sampled frames
    recursive: bool = True            # walk subfolders inside each root
    workers: int = 6                  # concurrent per-file workers


def load() -> Config:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        # Fallback: read from a project-local .env-style file if present.
        env_path = PROJECT_ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith("GEMINI_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Export it in your shell or add it to "
            f"{PROJECT_ROOT / '.env'} as GEMINI_API_KEY=...")

    # Service account is required for Drive sources but not for local-only
    # runs. We resolve the path here but leave existence-checking to the
    # caller so local-only invocations don't fail on a missing SA JSON.
    sa = os.environ.get("PS_SERVICE_ACCOUNT_JSON", DEFAULT_SA_PATH)
    sa = sa if Path(sa).is_file() else None

    max_files_env = os.environ.get("PS_MAX_FILES")
    max_files = int(max_files_env) if max_files_env else None

    folder_env = os.environ.get("PS_FOLDER_ID", "").strip()
    return Config(
        service_account_path=sa,
        gemini_api_key=key,
        pilot_folder_id=folder_env or None,
        output_dir=os.environ.get("PS_OUTPUT_DIR", DEFAULT_OUTPUT_DIR),
        media_cache_dir=os.environ.get("PS_MEDIA_CACHE", DEFAULT_MEDIA_CACHE),
        model=os.environ.get("PS_GEMINI_MODEL", DEFAULT_MODEL),
        max_files=max_files,
        recursive=os.environ.get("PS_RECURSIVE", "1").lower() not in ("0", "false", "no"),
        workers=max(1, int(os.environ.get("PS_WORKERS", "6"))),
    )
