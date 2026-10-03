"""Deterministic validation and filename assembly (V3.5).

V3.5 changes over V3.2:
  * Three new conditional filename tokens: CozyRiderColor, Cushion, Voiceover.
  * New filename field order:
      Category_Scene_DogType?_VehicleClass?_CozyRiderColor?_Cushion?_Voiceover?_ShotType_Format_T##?
  * Audio-analysis failure (audio_analysis_failed=True from classifier) blocks
    auto-rename with reason AudioAnalysisFailure — per V3.5 runtime rule
    "audio_analysis_failure_blocks_auto_rename".
  * Stem limit raised from 100 → 160 characters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import vocabulary as v


@dataclass
class ValidationResult:
    """Result of validating + assembling a proposed filename."""

    proposed_filename: Optional[str]
    status: str                          # AutoRenamed | NeedsReview | NeedsSplit | Unsupported | Unrelated
    review_reasons: List[str] = field(default_factory=list)
    tokens: Dict[str, Optional[str]] = field(default_factory=dict)
    format_ratio_error: Optional[float] = None
    # V3.0+ governance fields
    scene_runner_up: Optional[str] = None
    evidence_timestamps: List[float] = field(default_factory=list)
    classifier_version: Optional[str] = None

    @property
    def review_required(self) -> bool:
        return self.status != "AutoRenamed"


def validate_and_assemble(
    classification: Dict[str, Any],
    *,
    width: int,
    height: int,
    extension: str,
    duration_sec: float = 0.0,
    folder_category: Optional[str] = None,
    existing_take_stems: Optional[List[str]] = None,
) -> ValidationResult:
    """Validate an AI classification and assemble the final filename.

    ``classification`` matches the V3.5 output_schema: category, scene,
    dog_type, vehicle_class, cozy_rider_color, cushion, voiceover,
    audio_analysis_failed, shot_type, plus optional scene_runner_up,
    evidence_timestamps, classifier_version.

    ``duration_sec``: drives LongVideo routing.
    ``folder_category``: presumed Category from folder path.
    """
    reasons: List[str] = []
    tokens: Dict[str, Optional[str]] = {}
    classifier_version = classification.get("classifier_version")
    scene_runner_up = classification.get("scene_runner_up") or None
    evidence_timestamps = list(classification.get("evidence_timestamps") or [])

    def _pack(**over):
        return ValidationResult(
            classifier_version=classifier_version,
            scene_runner_up=scene_runner_up,
            evidence_timestamps=evidence_timestamps,
            **over,
        )

    # Version gate.
    if not v.VOCAB_VERSION_MATCHES:
        return _pack(
            proposed_filename=None,
            status="NeedsReview",
            review_reasons=["SchemaValidationFailed"],
            tokens={},
        )

    # 0. Relevance
    if classification.get("relevant") is False:
        return _pack(
            proposed_filename=None,
            status="Unrelated",
            review_reasons=[],
            tokens={},
        )

    # 1. Duration gate — >10min → LongVideo review, not split.
    if duration_sec and duration_sec > v.LONG_VIDEO_THRESHOLD_SEC:
        return _pack(
            proposed_filename=None,
            status="NeedsReview",
            review_reasons=["LongVideo"],
            tokens={},
        )

    # 2. Explicit multi-scene split.
    if classification.get("needs_split"):
        return _pack(
            proposed_filename=None,
            status="NeedsSplit",
            review_reasons=["MultipleMajorScenes"],
            tokens={},
        )

    # 3. V3.5 — audio-analysis failure blocks auto-rename.
    if classification.get("audio_analysis_failed"):
        return _pack(
            proposed_filename=None,
            status="NeedsReview",
            review_reasons=["AudioAnalysisFailure"],
            tokens={},
        )

    # 4. Category
    model_category = classification.get("category")

    if folder_category is not None:
        if folder_category not in v.CATEGORIES:
            reasons.append("SchemaValidationFailed")
            tokens["category"] = None
        elif classification.get("category_fit") is False:
            reasons.append("CategoryConflict")
            tokens["category"] = None
        elif model_category and model_category != folder_category:
            reasons.append("CategoryConflict")
            tokens["category"] = None
        elif model_category not in v.CATEGORIES:
            reasons.append("SchemaValidationFailed")
            tokens["category"] = None
        else:
            tokens["category"] = folder_category
    else:
        if model_category not in v.CATEGORIES:
            reasons.append("SchemaValidationFailed")
            tokens["category"] = None
        else:
            tokens["category"] = model_category

    # 5. Scene — hierarchical: must belong to the chosen Category.
    category = tokens.get("category")
    scene = classification.get("scene")
    if not category or scene not in (v.CATEGORY_SCENES.get(category) or set()):
        reasons.append("SchemaValidationFailed")
        tokens["scene"] = None
    else:
        tokens["scene"] = scene

    # 6. DogType
    dog_type = classification.get("dog_type")
    if dog_type is not None and dog_type not in v.DOG_TYPES:
        reasons.append("SchemaValidationFailed")
        dog_type = None
    tokens["dog_type"] = dog_type

    # 7. VehicleClass
    vehicle_class = classification.get("vehicle_class")
    if vehicle_class is not None and vehicle_class not in v.VEHICLE_CLASSES:
        reasons.append("SchemaValidationFailed")
        vehicle_class = None
    tokens["vehicle_class"] = vehicle_class

    # 8. V3.5 — CozyRiderColor (conditional, visual)
    cozy_rider_color = classification.get("cozy_rider_color")
    if cozy_rider_color is not None and cozy_rider_color not in v.COZY_RIDER_COLORS:
        reasons.append("SchemaValidationFailed")
        cozy_rider_color = None
    tokens["cozy_rider_color"] = cozy_rider_color

    # 9. V3.5 — Cushion (conditional, visual)
    cushion = classification.get("cushion")
    if cushion is not None and cushion not in v.CUSHION_TOKENS:
        reasons.append("SchemaValidationFailed")
        cushion = None
    tokens["cushion"] = cushion

    # 10. V3.5 — Voiceover (conditional, audio)
    voiceover = classification.get("voiceover")
    if voiceover is not None and voiceover not in v.VOICEOVER_TOKENS:
        reasons.append("SchemaValidationFailed")
        voiceover = None
    tokens["voiceover"] = voiceover

    # 11. ShotType — required.
    shot_type = classification.get("shot_type")
    if shot_type not in v.SHOT_TYPES:
        reasons.append("SchemaValidationFailed")
        shot_type = None
    tokens["shot_type"] = shot_type

    # 12. Format — deterministic from dimensions.
    fmt, actual_ratio, rel_err = v.pick_format(width, height)
    if fmt is None:
        reasons.append("FormatUnmatched")
    tokens["format"] = fmt

    # 13. Extension.
    ext = (extension or "").lower()
    if not ext.startswith("."):
        ext = "." + ext if ext else ""

    required_ok = all([tokens["category"], tokens["scene"], tokens["shot_type"],
                       tokens["format"], ext])
    if not required_ok or reasons:
        return _pack(
            proposed_filename=None,
            status="NeedsReview",
            review_reasons=_dedupe(reasons or ["SchemaValidationFailed"]),
            tokens=tokens,
            format_ratio_error=rel_err if fmt is None else None,
        )

    # 14. Assemble — V3.5 field order:
    #   Category_Scene_DogType?_VehicleClass?_CozyRiderColor?_Cushion?_Voiceover?_ShotType_Format
    parts: List[str] = [tokens["category"], tokens["scene"]]
    if tokens["dog_type"]:
        parts.append(tokens["dog_type"])
    if tokens["vehicle_class"]:
        parts.append(tokens["vehicle_class"])
    if tokens["cozy_rider_color"]:
        parts.append(tokens["cozy_rider_color"])
    if tokens["cushion"]:
        parts.append(tokens["cushion"])
    if tokens["voiceover"]:
        parts.append(tokens["voiceover"])
    parts.extend([tokens["shot_type"], tokens["format"]])
    base_stem = "_".join(parts)

    # 15. Take-collision handling (single-file convenience path).
    take = None
    if existing_take_stems and base_stem in existing_take_stems:
        take = _next_take(existing_take_stems, base_stem)

    stem = base_stem if take is None else f"{base_stem}_{take}"
    if len(stem) > v.FILENAME_STEM_MAX_CHARS:
        return _pack(
            proposed_filename=None,
            status="NeedsReview",
            review_reasons=["FilenameTooLong"],
            tokens=tokens,
        )

    filename = stem + ext
    return _pack(
        proposed_filename=filename,
        status="AutoRenamed",
        review_reasons=[],
        tokens=tokens,
        format_ratio_error=rel_err,
    )


def _dedupe(items: List[str]) -> List[str]:
    seen, out = set(), []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _next_take(existing: List[str], base_stem: str) -> str:
    used = set()
    prefix = base_stem + "_T"
    for s in existing:
        if s == base_stem:
            used.add(1)
        elif s.startswith(prefix):
            tail = s[len(prefix):]
            if tail.isdigit():
                used.add(int(tail))
    n = 1
    while n in used:
        n += 1
    return f"T{n:02d}"
