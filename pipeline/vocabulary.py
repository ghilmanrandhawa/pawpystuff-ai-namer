"""Controlled vocabulary loader for PawpyStuff Creator Content Naming
Convention V3.0+.

V3.0 introduced a machine-readable taxonomy JSON alongside the human DOCX SOP.
This module loads that JSON as the single source of truth — no more hand-porting
Category/Scene lists across versions.

To upgrade: drop a new taxonomy JSON in the project root and bump
``EXPECTED_VOCAB_VERSION``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, FrozenSet, List, Tuple

# ---------------------------------------------------------------------------
# Version pinning
# ---------------------------------------------------------------------------
# The runtime and the taxonomy MUST agree. Per V3.0's
# ``runtime_rules.version_mismatch_blocks_auto_rename``, we refuse to
# auto-rename when they don't.
EXPECTED_VOCAB_VERSION = "3.5"
AUTOMATION_VERSION = "0.6.0-v35"

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _find_taxonomy_file() -> Path:
    """Prefer the JSON matching EXPECTED_VOCAB_VERSION; otherwise take the
    lexically newest ``PawpyStuff_Creator_Content_Taxonomy_V*.json``."""
    override = os.environ.get("PS_TAXONOMY_JSON")
    if override:
        p = Path(override)
        if not p.is_file():
            raise FileNotFoundError(f"PS_TAXONOMY_JSON points at missing file: {p}")
        return p
    pinned = PROJECT_ROOT / f"PawpyStuff_Creator_Content_Taxonomy_V{EXPECTED_VOCAB_VERSION}.json"
    if pinned.is_file():
        return pinned
    candidates = sorted(PROJECT_ROOT.glob("PawpyStuff_Creator_Content_Taxonomy_V*.json"))
    if not candidates:
        raise FileNotFoundError(
            f"No PawpyStuff_Creator_Content_Taxonomy_V*.json found in {PROJECT_ROOT}"
        )
    return candidates[-1]


TAXONOMY_FILE = _find_taxonomy_file()
with TAXONOMY_FILE.open() as _fh:
    _TAX = json.load(_fh)

VOCABULARY_VERSION: str = _TAX["vocabulary_version"]
VOCAB_VERSION_MATCHES: bool = VOCABULARY_VERSION == EXPECTED_VOCAB_VERSION

FILENAME_STRUCTURE: str = _TAX["filename_structure"]


# ---------------------------------------------------------------------------
# Category × Scene allow-list (derived from JSON)
# ---------------------------------------------------------------------------

CATEGORY_SCENES: Dict[str, FrozenSet[str]] = {
    cat: frozenset(spec["scenes"])
    for cat, spec in _TAX["categories"].items()
}
CATEGORIES: FrozenSet[str] = frozenset(CATEGORY_SCENES.keys())

CATEGORY_DEFINITIONS: Dict[str, str] = {
    cat: spec["definition"] for cat, spec in _TAX["categories"].items()
}

SCENE_DEFINITIONS: Dict[str, str] = _TAX.get("scene_definitions", {})

# Ordered decision rules for Category: Problem → ProductUse → ProductShot.
CATEGORY_DECISION_ORDER: List[str] = list(_TAX.get("category_decision_order", []))


# ---------------------------------------------------------------------------
# DogType / VehicleClass / ShotType / Format
# ---------------------------------------------------------------------------

DOG_TYPES: FrozenSet[str] = frozenset(_TAX["dog_types"])

# ---------------------------------------------------------------------------
# V3.5 new conditional filename tokens
# ---------------------------------------------------------------------------

COZY_RIDER_COLORS: FrozenSet[str] = frozenset(_TAX.get("cozy_rider_colors", []))
VOICEOVER_TOKENS:  FrozenSet[str] = frozenset(_TAX.get("voiceover_tokens", []))
CUSHION_TOKENS:    FrozenSet[str] = frozenset(_TAX.get("cushion_tokens", []))
# Everything except OtherDog / MultiDog counts as a specific breed.
DOG_BREEDS: FrozenSet[str] = DOG_TYPES - {"OtherDog", "MultiDog"}

VEHICLE_CLASSES: FrozenSet[str] = frozenset(_TAX["vehicle_classes"])

SHOT_TYPES: FrozenSet[str] = frozenset(_TAX["shot_types"])
# Deterministic precedence per V3.0 shot_type_priority.
SHOT_PRIORITY: List[str] = list(_TAX.get("shot_type_priority", []))

FORMATS: Dict[str, float] = {
    tok: (int(tok.split("x")[0]) / int(tok.split("x")[1])) for tok in _TAX["formats"]
}
FORMAT_TOLERANCE = 0.03  # ±3% per SOP


# ---------------------------------------------------------------------------
# Runtime rules
# ---------------------------------------------------------------------------

RUNTIME_RULES: Dict[str, bool] = _TAX.get("runtime_rules", {})
NEEDS_SPLIT_RULE: str = _TAX.get("needs_split_rule", "")
LONG_VIDEO_RULE: str = _TAX.get("long_video_rule", "")


# ---------------------------------------------------------------------------
# Review reasons + system errors
# ---------------------------------------------------------------------------

REVIEW_REASONS: FrozenSet[str] = frozenset(_TAX.get("review_reasons", []))
SYSTEM_ERROR_EXAMPLES: FrozenSet[str] = frozenset(_TAX.get("system_error_examples", []))


# ---------------------------------------------------------------------------
# Terminal statuses (not in JSON — pipeline-internal)
# ---------------------------------------------------------------------------

STATUSES: FrozenSet[str] = frozenset({
    "AutoRenamed", "NeedsReview", "NeedsSplit", "Approved", "Edited",
    "Rejected", "Duplicate", "Unsupported", "Unrelated", "RolledBack",
})


# ---------------------------------------------------------------------------
# Media / sampling defaults (not in taxonomy JSON — engineering knobs)
# ---------------------------------------------------------------------------

SUPPORTED_IMAGE_EXTENSIONS: FrozenSet[str] = frozenset({".jpg", ".jpeg", ".png", ".heic"})
SUPPORTED_VIDEO_EXTENSIONS: FrozenSet[str] = frozenset({".mp4", ".mov"})
SUPPORTED_EXTENSIONS: FrozenSet[str] = SUPPORTED_IMAGE_EXTENSIONS | SUPPORTED_VIDEO_EXTENSIONS

# V3.0: >10 min routes to NeedsReview with reason LongVideo — no auto NeedsSplit.
LONG_VIDEO_THRESHOLD_SEC = 10 * 60
SHORT_VIDEO_MAX_SEC = 30
SHORT_VIDEO_FRAME_COUNT = 6
LONG_VIDEO_FRAME_COUNT = 20   # was 12; bumped for better multi-scene coverage on testimonials
FILENAME_STEM_MAX_CHARS = 160


# ---------------------------------------------------------------------------
# Camera movement (metadata only, not in filename)
# ---------------------------------------------------------------------------

CAMERA_MOVEMENTS: FrozenSet[str] = frozenset({
    "Static", "Handheld", "Tracking", "Pan", "Tilt", "Zoom", "SlowMotion", "Timelapse",
})


# ---------------------------------------------------------------------------
# Output schema (informational — mirrors the JSON's output_schema section)
# ---------------------------------------------------------------------------

OUTPUT_SCHEMA_FIELDS: Dict[str, str] = _TAX.get("output_schema", {})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_valid_category_scene(category: str, scene: str) -> bool:
    """True iff (category, scene) is in the approved allow-list."""
    return category in CATEGORY_SCENES and scene in CATEGORY_SCENES[category]


def pick_format(width: int, height: int) -> Tuple[str | None, float, float]:
    """Return (format_token, actual_ratio, relative_error) or (None, ratio, err)
    if no approved ratio matches within ±FORMAT_TOLERANCE."""
    if not width or not height:
        return None, 0.0, float("inf")
    actual = width / height
    best_token = None
    best_err = float("inf")
    for token, target in FORMATS.items():
        err = abs(actual - target) / target
        if err < best_err:
            best_err = err
            best_token = token
    if best_err <= FORMAT_TOLERANCE:
        return best_token, actual, best_err
    return None, actual, best_err
