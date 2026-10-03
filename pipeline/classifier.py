"""Gemini classifier (V3.0). Returns structured JSON strictly bound to the
V3.0 controlled vocabulary.

V3.0 changes:
  * Categories redefined — ProductShot now covers ALL product/creator-led
    footage (setup, cleaning, testimonials, groceries); ProductUse is
    dog-led only; Problem unchanged.
  * Hierarchical inference: pick Category first, then Scene must belong to
    that Category's allow-list.
  * Deterministic ShotType priority: DashCam > POV > Overhead > LowAngle >
    OTS > framing (ECU/CU/Mid/Wide).
  * NeedsSplit only when 2+ segments have DIFFERENT primary Scenes AND
    would be independently useful editor assets. Length alone does not split.
  * >10 min videos route to LongVideo review, not auto NeedsSplit.
  * Confidence thresholds are gone; replaced with evaluation-calibrated
    production gates on a 500-asset gold set (client-side, not runtime).
  * Output schema adds scene_runner_up, evidence_timestamps, classifier_version.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

from . import vocabulary as v
from .media import SampledMedia

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_MODEL = "gemini-flash-latest"

# Bump whenever the prompt semantics change materially. Written into every
# per-file log for reproducibility (V3.0 governance requirement).
CLASSIFIER_VERSION = f"{DEFAULT_MODEL}+prompt-v35-02-audio-fix"

# Video-input caps: send full video only when clip is short enough that the
# inline_data payload stays under Gemini's per-request limit.
VIDEO_INPUT_MAX_SEC = 30
VIDEO_INPUT_MAX_BYTES = 15 * 1024 * 1024

# Shots we adjudicate with a focused second pass. These are where our top-1
# most often disagrees with the client answer key (Wide vs Mid vs POV).
ADJUDICATE_SHOTS = {"Wide", "Mid", "POV"}


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

_SHOT_DEFS = {
    "CU":      "Close-up. Subject fills most of the frame with only minimal surrounding context.",
    "Mid":     "Primary subject fills most of the frame while enough immediate context remains visible to understand the scene.",
    "Wide":    "Full or nearly full primary subject visible along with substantial vehicle or environmental context (seat backs, door frames, ceiling, or other passengers visible).",
    "OTS":     "Camera behind/over a subject's shoulder, with the shoulder or head functioning as foreground framing toward another subject or point of interest.",
    "POV":     "Camera intentionally represents the recorder/owner's first-person viewpoint. Handheld/moving alone is NOT POV — needs visible first-person cues (recorder's own hands, chest cam, etc.).",
    "DashCam": "Fixed dashboard/cabin-facing camera perspective during travel (highest priority when applicable).",
}


def _vocab_block() -> str:
    lines: List[str] = []
    lines.append(f"VocabularyVersion: {v.VOCABULARY_VERSION}")
    lines.append(f"FilenameStructure: {v.FILENAME_STRUCTURE}")
    lines.append("")
    lines.append("APPROVED CATEGORIES (with definitions):")
    for cat in ["Problem", "ProductUse", "ProductShot"]:
        if cat not in v.CATEGORIES: continue
        lines.append(f"  * {cat}: {v.CATEGORY_DEFINITIONS.get(cat,'')}")
    lines.append("")
    lines.append("CATEGORY DECISION ORDER (evaluate in this order, first match wins):")
    for i, rule in enumerate(v.CATEGORY_DECISION_ORDER, 1):
        lines.append(f"  {i}. {rule}")
    lines.append("")
    lines.append("ALLOWED SCENES PER CATEGORY (Scene MUST belong to the chosen Category):")
    for cat in ["Problem", "ProductUse", "ProductShot"]:
        if cat not in v.CATEGORY_SCENES: continue
        lines.append(f"  {cat}:")
        for scene in sorted(v.CATEGORY_SCENES[cat]):
            defn = v.SCENE_DEFINITIONS.get(scene, "").strip()
            if defn:
                lines.append(f"    - {scene}: {defn}")
            else:
                lines.append(f"    - {scene}")
    lines.append("")
    lines.append(f"APPROVED DogType tokens: {', '.join(sorted(v.DOG_TYPES))}")
    lines.append(f"APPROVED VehicleClass tokens: {', '.join(sorted(v.VEHICLE_CLASSES))}")
    lines.append(f"APPROVED CozyRiderColor tokens: {', '.join(sorted(v.COZY_RIDER_COLORS))} (or null)")
    lines.append(f"APPROVED Voiceover tokens: {', '.join(sorted(v.VOICEOVER_TOKENS))} (or null)")
    lines.append(f"APPROVED Cushion tokens: {', '.join(sorted(v.CUSHION_TOKENS))} (or null)")
    lines.append("")
    lines.append("APPROVED ShotType tokens (definitions):")
    for st in sorted(v.SHOT_TYPES):
        lines.append(f"  - {st}: {_SHOT_DEFS.get(st,'')}")
    lines.append("")
    lines.append(f"SHOT PRIORITY (evaluate in order; use the first that clearly applies): "
                 f"{' > '.join(v.SHOT_PRIORITY)}")
    lines.append("")
    lines.append(f"NeedsSplit rule: {v.NEEDS_SPLIT_RULE}")
    lines.append(f"LongVideo rule: {v.LONG_VIDEO_RULE}")
    return "\n".join(lines)


SYSTEM_PROMPT = f"""\
You are the CLASSIFICATION component of the PawpyStuff Creator Content
Naming Convention automation (Vocabulary V{v.VOCABULARY_VERSION}).
PawpyStuff sells CozyRider — a dog-travel product line (seat covers,
hammocks, hard-bottom platforms, mesh windows, door protectors, carry
cases) for cars, SUVs, trucks, and minivans.

You are shown one asset at a time. For videos you receive representative
frames AND, when available, audio (as a separate audio part or embedded in
the video). Use BOTH visual and audio evidence together — audio is the
primary signal for Voiceover and for identifying off-camera Testimonials.
All frames come from the SAME asset. Return STRUCTURED JSON ONLY. Never
write a filename. Never invent tokens.

============================================================
CORE RULES
============================================================

1. HIERARCHICAL INFERENCE. Pick CATEGORY first (Problem, ProductUse, or
   ProductShot), then pick a SCENE that is allowed under that Category.
   NEVER pick a Scene that isn't in the chosen Category's allow-list.

2. CATEGORY DECISION ORDER (evaluate top-down; first match wins):
   a. Problem — if pain/failure/failed alternative/before-after problem
      is the primary beat.
   b. ProductUse — if the dog is ACTIVELY using or experiencing CozyRider
      as the primary beat (including direct owner-dog interactions like
      OwnerPetsDog, OwnerBucklesDog, OwnerLoadsDog).
   c. Otherwise ProductShot.

3. MAIN AI RULE (V2.9 clarification, still active):
   If it's not a Problem AND the dog is NOT actively using the product,
   it should generally be ProductShot — even when a person is on-camera
   demonstrating, setting up, cleaning, testifying, unpacking, etc.
   A dog simply appearing in the background does NOT make it ProductUse.

4. ONLY use approved tokens. If a Scene you'd want doesn't exist, pick
   the CLOSEST approved token in the correct Category and put your
   second-best guess in `scene_runner_up`.

5. SCENE = the single primary editor-search intent visible across the
   frames. A brief secondary action is not the primary Scene.

6. SHOT TYPE — apply this deterministic priority, but each specialty
   shot has a HIGH BAR. Default to framing (ECU/CU/Mid/Wide) unless a
   specialty shot clearly qualifies.

   * DashCam (HIGH BAR): the camera is CLEARLY mounted or fixed to the
     vehicle dashboard/windshield/cabin with a stable, unmoving frame
     during travel. Handheld phone footage inside a moving car is NOT
     DashCam. If the operator is holding the camera, use framing instead.

   * POV (HIGH BAR): the camera clearly represents a SPECIFIC PERSON'S
     own first-person viewpoint. To qualify, at LEAST ONE must be true:
       - the recorder's own hands, arms, or feet enter the frame from
         the recorder's own perspective (not filmed by a third party);
       - the camera is body-mounted (visible chest strap, head-cam);
       - the framing is unambiguously eye-level looking down at the
         recorder's own body/actions.
     If someone is just filming their own hands doing a task from a
     bystander angle, or holding a phone while loading groceries by hand,
     that is NOT POV. Prefer Wide/Mid instead.

   * Overhead / LowAngle: dominant vertical angle. Overhead = clearly
     looking straight down. LowAngle = camera at or below platform level
     with the subject towering above.

   * OTS: shoulder/head clearly acts as foreground framing toward
     another subject. Just seeing someone from behind is NOT OTS.

   * OTHERWISE FRAMING (default path — prefer these when unsure):
     - Wide: substantial vehicle/environmental context visible around
       the subject (whole backseat + surroundings, whole vehicle visible).
       When you can see the seat backs, door frames, windows, ceiling,
       or other passengers, choose Wide even if the subject is central.
     - Mid: subject dominates the frame with only immediate context.
     - CU: subject occupies most of the frame, minimal context.

   BLACK SCREEN / AUDIO-ONLY ASSETS: when frames are blank, black, or
   contain no meaningful visual content (e.g. a voiceover hook recorded
   over a black card), use "Mid" as the default ShotType. Never return
   null for shot_type.

   TIE-BREAKER: when torn between two shots (e.g. "could be POV or
   Wide"), pick the framing (Wide/Mid/CU) not the specialty. Specialty
   shots require unambiguous evidence.

7. NEEDS_SPLIT — set true ONLY when two or more separate segments would
   receive DIFFERENT primary Scene classifications AND each segment
   would be an independently useful editor asset. Length, repeated
   examples of one Scene, or framing changes ALONE do NOT split.
   * Multi-step SETUP sequences: SetupFullProduct is the correct SINGLE
     Scene — do NOT split.
   * Multi-feature TESTIMONIALS: Testimonial is the correct SINGLE Scene
     when creator-to-camera talking is the dominant beat.

8. LONG VIDEO — if the clip is longer than 10 minutes, do NOT set
   needs_split. The pipeline will route it to LongVideo review.

9. DogType: only when a dog is visibly present. Use MultiDog when
   multiple dogs are the primary subject. Use OtherDog when the breed
   is unlisted, mixed, or uncertain. Never guess a breed.

10. VehicleClass: applies across all Categories including ProductShot.
    Never downgrade a full-size pickup to Truck; if unsure of size,
    omit VehicleClass.

11. VOICEOVER (V3.5 — audio required):
    * Listen to the audio track of the video (or the separate audio part
      when provided alongside frames).
    * Set `voiceover` = "Voiceover" when you detect ≥ 3 seconds total of
      clear intelligible speech, whether the speaker is ON-camera or
      OFF-camera. Background music, ambient noise, and dog sounds do NOT
      count as speech.
    * Set `voiceover` = null when there is no intelligible speech or less
      than 3 seconds total.
    * Set `audio_analysis_failed` = true ONLY when you genuinely could not
      analyze the audio (e.g., audio was absent from the payload or
      technically unreadable). Do NOT use this for muted videos or
      videos where you simply detected no speech — those are valid null
      Voiceover results. If the asset is a still image, set
      `audio_analysis_failed` = false and `voiceover` = null.

12. COZY_RIDER_COLOR (V3.5 — visual only):
    * Identify the color of the CozyRider hard-bottom seat cover itself.
    * Set to "BlackCover" when the primary cover surface is solid black.
    * Set to "BlackCoverBrownTrim" when black cover with visible brown trim.
    * Set to "GreyCover" when the primary cover surface is grey/gray.
    * Set to null when the cover is not clearly visible, the color is
      uncertain, or when what's visible is the vehicle upholstery, carry
      case, cushion, or a competitor cover.

13. CUSHION (V3.5 — visual only):
    * Set to "Cushion" only when the white-and-black checkered CozyCushion
      accessory is visibly present alongside the CozyRider in the asset.
    * Set to null otherwise. Do not treat an unrelated blanket, pad, or
      dog bed as Cushion.

14. Provide `evidence_timestamps` (seconds within the clip) pointing to
    the frames that most strongly justified your Scene decision.

15. Provide `scene_runner_up` — the SECOND-BEST allowed Scene for the
    chosen Category, or null if no reasonable alternative.

16. Return valid JSON matching the schema. Do NOT wrap in markdown.

============================================================
HIGH-VALUE SCENE DISAMBIGUATION (few-shot visual cues)
============================================================

FailedSeatCover (Problem):
  * VISIBLE CUES: thin flimsy fabric, competitor/generic hammock covers
    (NOT branded CozyRider), visible ripping/tearing, "cheap-looking"
    quilted or fabric surface, hammock sagging/collapsing into the
    footwell gap, creator ripping/tossing/inspecting a cover with
    disapproval. Owner narrating a rejection.
  * Do NOT use for the branded CozyRider platform. Only for INFERIOR/
    ALTERNATIVE covers being criticized or demonstrated as failing.
  * If the filename says "cheap hammock", "trashy cover", "failed cover"
    etc., this is almost certainly FailedSeatCover.

WeightCapacityTest (ProductShot):
  * VISIBLE CUES: a person deliberately stepping, sitting, or kneeling
    on the CozyRider hard-bottom platform, or stacking weights/objects
    on it, to prove structural strength. Multiple people on the platform.
    Downward pressure demonstrations.
  * Do NOT use for a dog simply riding on the platform (that's DogRiding).
  * Do NOT use for someone stepping on a COMPETITOR product to fail it
    (that's Problem/SeatCoverSags or Problem/FailedSeatCover).

Testimonial (V3.5 DUAL-CATEGORY — audio is primary signal):
  * The creator need NOT be visible on-camera. Off-camera voiceover
    commentary about CozyRider qualifies as Testimonial.
  * Use audio evidence first: ~10 or more seconds of continuous or
    recurring clear product commentary is strong evidence. Shorter clips
    may qualify when the spoken testimonial clearly dominates.
  * Category depends on whether a dog is actively using CozyRider:
      - ProductUse_Testimonial: sustained creator commentary is dominant
        WHILE a dog is actively using/occupying CozyRider.
      - ProductShot_Testimonial: sustained creator commentary is dominant
        and no dog is actively using the product (dog may be absent or
        present but not occupying CozyRider).
  * VISIBLE/AUDIO CUES: creator faces the camera and talks directly to it;
    or creator's voice is clearly heard off-camera narrating CozyRider
    experience; hands may gesture; brief handling, pointing, sitting on the
    product, or showing a feature does NOT override Testimonial.
  * If a physical product demonstration is clearly the dominant action and
    speech only narrates it, keep the specific demonstration Scene and set
    voiceover = "Voiceover" instead.

Testimonial vs WeightCapacityTest (V3.5 TIE-BREAKER):
  * Use Testimonial when sustained creator commentary about CozyRider is the
    dominant content, even if the creator briefly sits on, presses, points
    to, or handles the platform.
  * Use WeightCapacityTest ONLY when deliberate visible load-bearing proof
    is the dominant action (person/dog/weight sustained on platform as the
    primary beat).
  * Speech may accompany a genuine load test — track Voiceover separately.

SetupFullProduct (ProductShot):
  * VISIBLE CUES: a coherent sequence of MULTIPLE distinct setup steps in
    one clip: unfolding + placing + strapping + adjusting. Different
    setup actions across the frame timeline.
  * Do NOT set needs_split for these — SetupFullProduct is the correct
    single Scene.
  * If ONLY ONE setup step is visible (e.g. only strap-attachment),
    pick the specific Setup* scene instead.

WeakPlasticClips (Problem):
  * VISIBLE CUES: cheap plastic clip hardware being shown, tested,
    pulled at, criticized, or visibly weak/broken. Not the CozyRider
    metal clips.
  * If filename mentions "plastic clips", "cheap clips", almost certainly
    WeakPlasticClips.

BeforeAfterFailedSeatCoverVsPlatform (Problem):
  * VISIBLE CUES: side-by-side or sequential comparison of a
    failed/cheap cover VS the CozyRider platform. Often testimonial
    with a "before/after" narrative.
  * If filename mentions "before after", this is almost certainly it.

DogRiding vs DogSitting/Standing/Lying:
  * If the vehicle is IN MOTION (windows show scenery moving, driving
    footage, dashcam angle) and dog is on the platform, prefer DogRiding
    over the posture-specific scenes.
  * DogSitting/Standing/Lying only when the vehicle is CLEARLY stationary
    and the specific posture is the primary creative point.

============================================================
HIGH-VALUE SHOT TYPE DISAMBIGUATION (few-shot)
============================================================

Wide vs Mid — the most common confusion:
  * Choose WIDE when you can see any of: full backseat area with door frames,
    ceiling/roof lining, windows on both sides, front seat backs and rear seats
    simultaneously, other passengers or a large portion of the car interior,
    or the whole outside of the vehicle. The subject can still be central —
    Wide is about how much CONTEXT surrounds it, not how small the subject is.
  * Choose MID when the subject dominates the frame and only immediate
    context is visible (e.g., subject + adjacent seat cushion, but not the
    whole backseat / doors / ceiling).
  * TIE-BREAKER: on backseat clips where you can see the whole backseat area
    plus doors or ceiling, choose Wide. Reserve Mid for tighter compositions.

POV — STRICT bar:
  * POV requires at least ONE of these visible in the frames:
    - The RECORDER's own hand, arm, or foot entering the frame FROM the
      recorder's perspective (e.g., a hand extending outward from behind
      the camera, not filmed by a third party at 90° from the side).
    - A body-mounted camera setup (visible chest strap, head-cam frame,
      handlebar mount).
    - Framing that could only come from the recorder's own eyeline
      looking at their own body/actions.
  * Just being CLOSE to the action is NOT POV. Just holding a phone at
    arm's length is NOT POV. Grocery loading where the hand angle is
    ambiguous is NOT POV — use Wide or Mid instead.

DashCam — HIGH bar:
  * DashCam requires a CLEARLY mounted, unmoving camera fixed to the
    dashboard/windshield/cabin during travel. The horizon and window
    frame should be perfectly stable across all frames.
  * Handheld phone footage inside a moving car is NOT DashCam — if the
    frame wobbles, panics, or shifts angle between frames, use framing
    (Wide/Mid) instead.

OTS — STRICT bar:
  * OTS requires a shoulder/head clearly acting as foreground framing.
    Just seeing someone from behind is NOT OTS.

CU vs ECU vs Mid:
  * ECU: subject detail fills the whole frame (e.g., closeup of just
    the metal clip hardware, or just the fabric texture).
  * CU: subject fills most of the frame with minimal context (e.g.,
    just the dog's face + a tiny bit of surroundings).
  * Mid: subject dominates with immediate context.

TIE-BREAKER: When torn between a specialty shot (DashCam / POV / OTS /
Overhead / LowAngle) and framing (Wide / Mid / CU), always prefer
framing. Specialty shots require unambiguous evidence.

ProductShot framing — CRITICAL RULE:
  * When Category=ProductShot (product hardware being shown, NO dog using
    it), the shot is almost NEVER POV. A tight close-up of a seatbelt,
    anchor, clip, zipper, patch, or fabric — even when filmed from the
    driver's seat angle looking down at the product — is CU (or ECU),
    NOT POV. POV requires evidence the recorder is doing the ACTIVITY
    (petting a dog, driving, buckling something), not merely inspecting
    a static product.
  * ProductShot files showing product hardware close-up (Seatbelt,
    SeatAnchors, DoorProtectors, MetalClips, MeshZip, WaterAbsorbent,
    Durability, hardware close-ups) → default to CU when the product
    fills most of the frame; Mid when the product + immediate context is
    visible; Wide ONLY if you can see the whole backseat area with door
    frames or ceiling. Do NOT choose POV for product-focused shots.
  * If the frame shows product hardware with a hand touching/holding it
    for scale, that is still CU or Mid — NOT POV. POV needs the
    recorder's FIRST-PERSON action, not third-party hands on product.

============================================================
DETAILED VOCABULARY
============================================================

{_vocab_block()}
"""


# ---------------------------------------------------------------------------
# Response schema
# ---------------------------------------------------------------------------

_CATEGORY_ENUM = sorted(v.CATEGORIES) + [None]
_DOG_ENUM = sorted(v.DOG_TYPES) + [None]
_VEHICLE_ENUM = sorted(v.VEHICLE_CLASSES) + [None]
_SHOT_ENUM = sorted(v.SHOT_TYPES) + [None]
_MOVEMENT_ENUM = sorted(v.CAMERA_MOVEMENTS) + [None]
_COLOR_ENUM = sorted(v.COZY_RIDER_COLORS) + [None]
_VOICEOVER_ENUM = sorted(v.VOICEOVER_TOKENS) + [None]
_CUSHION_ENUM = sorted(v.CUSHION_TOKENS) + [None]

RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "needs_split": {"type": "boolean"},
        "folder_category": {"type": "string", "nullable": True, "enum": _CATEGORY_ENUM},
        "category": {"type": "string", "nullable": True, "enum": _CATEGORY_ENUM},
        "category_fit": {"type": "boolean"},
        "suggested_category": {"type": "string", "nullable": True, "enum": _CATEGORY_ENUM},
        "scene": {"type": "string", "nullable": True},
        "scene_runner_up": {"type": "string", "nullable": True},
        "dog_present": {"type": "boolean"},
        "dog_type": {"type": "string", "nullable": True, "enum": _DOG_ENUM},
        "vehicle_visible": {"type": "boolean"},
        "vehicle_class": {"type": "string", "nullable": True, "enum": _VEHICLE_ENUM},
        # V3.5 new conditional tokens
        "cozy_rider_color": {"type": "string", "nullable": True, "enum": _COLOR_ENUM},
        "cushion": {"type": "string", "nullable": True, "enum": _CUSHION_ENUM},
        "voiceover": {"type": "string", "nullable": True, "enum": _VOICEOVER_ENUM},
        "audio_analysis_failed": {"type": "boolean"},
        "shot_type": {"type": "string", "nullable": True, "enum": _SHOT_ENUM},
        "camera_movement": {"type": "string", "nullable": True, "enum": _MOVEMENT_ENUM},
        "secondary_actions": {"type": "array", "items": {"type": "string"}},
        "evidence_timestamps": {"type": "array", "items": {"type": "number"}},
        "reasoning": {"type": "string"},
    },
    "required": [
        "relevant", "needs_split", "category", "category_fit", "scene",
        "dog_present", "vehicle_visible", "shot_type",
        "voiceover", "audio_analysis_failed",
    ],
}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

@dataclass
class ClassifierResult:
    classification: Dict[str, Any]
    raw_response: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    model: str = DEFAULT_MODEL
    usage: Dict[str, Any] = field(default_factory=dict)
    classifier_version: str = CLASSIFIER_VERSION


class GeminiClassifier:
    """Multimodal classifier bound to the V3.0 controlled vocabulary."""

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, timeout: int = 120):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def classify(
        self,
        media: SampledMedia,
        *,
        folder_category: Optional[str] = None,
        user_hint: Optional[str] = None,
        filename_hint: Optional[str] = None,
        video_bytes: Optional[bytes] = None,
        audio_bytes: Optional[bytes] = None,
        reference_frames: Optional[List[tuple[bytes, str]]] = None,
    ) -> ClassifierResult:
        """Classify one asset.

        ``video_bytes``: when supplied AND the clip is short enough, sends the
        full video (audio already embedded — no separate audio_bytes needed).

        ``audio_bytes``: MP3 audio extracted from longer clips where we fall
        back to frames-only mode. Sent as a separate inline_data audio part so
        Gemini can analyze speech for the Voiceover field.

        ``reference_frames``: (jpeg_bytes, label_text) few-shot examples.
        """
        if not media.frames_jpeg:
            return ClassifierResult(
                classification={},
                error=media.unsupported_reason or "no frames",
                model=self.model,
            )

        header = [
            f"Asset kind: {'video' if media.probe.duration_sec > 0 else 'image'}.",
            f"Original dimensions: {media.probe.width}x{media.probe.height}.",
            f"Duration: {media.probe.duration_sec:.2f}s.",
            f"Frames sampled: {len(media.frames_jpeg)} "
            f"(timestamps sec: {[round(t,2) for t in media.frame_timestamps_sec]}).",
        ]
        if folder_category:
            header.append(
                f"FOLDER CATEGORY (presumed): {folder_category}. "
                f"Validate fit; do NOT silently reclassify."
            )
        else:
            header.append(
                f"FOLDER CATEGORY: unknown (pilot mode). Choose `category` from "
                f"{sorted(v.CATEGORIES)} using the Category decision order."
            )
        if user_hint:
            header.append(f"Operator hint: {user_hint}")
        if filename_hint:
            # Filenames often contain the creator's own context (e.g. "cheap
            # hammock cover" → FailedSeatCover). Treat as a weak prior — model
            # should still trust its own visual analysis when they conflict.
            header.append(
                f"Original filename (LOW-priority context; visual evidence "
                f"still authoritative): {filename_hint!r}"
            )

        parts: List[Dict[str, Any]] = [{"text": "\n".join(header)}]

        # Reference frames (few-shot). Each is one (jpeg, label_text) pair.
        # Emitting these before the target media so the model sees "here's
        # what Wide looks like for this creator" before it decides.
        if reference_frames:
            parts.append({"text": "REFERENCE EXAMPLES (from prior labeled clips by this creator; use as visual anchors when classifying the target below):"})
            for ref_jpeg, ref_label in reference_frames:
                parts.append({"text": f"Example: {ref_label}"})
                parts.append({
                    "inline_data": {
                        "mime_type": "image/jpeg",
                        "data": base64.b64encode(ref_jpeg).decode("ascii"),
                    }
                })
            parts.append({"text": "END OF REFERENCE EXAMPLES.\n\nNow classify the TARGET asset below."})

        # Target media — full video when short enough (audio included), else
        # sampled frames + separate audio for Voiceover analysis.
        use_video = (
            video_bytes is not None
            and 0 < media.probe.duration_sec <= VIDEO_INPUT_MAX_SEC
            and len(video_bytes) <= VIDEO_INPUT_MAX_BYTES
        )
        if use_video:
            parts.append({
                "text": (
                    "TARGET (full video — audio is embedded; use temporal/motion "
                    "cues for ShotType and listen to audio for Voiceover):"
                )
            })
            parts.append({
                "inline_data": {
                    "mime_type": media.probe.mime_type or "video/mp4",
                    "data": base64.b64encode(video_bytes).decode("ascii"),
                }
            })
        else:
            # Frames-only path: inject extracted audio separately so Gemini
            # can detect speech for the Voiceover field even on long clips.
            if audio_bytes:
                parts.append({
                    "text": (
                        "TARGET AUDIO (listen for intelligible speech ≥3 sec "
                        "to determine Voiceover; on-camera or off-camera speech counts):"
                    )
                })
                parts.append({
                    "inline_data": {
                        "mime_type": "audio/mpeg",
                        "data": base64.b64encode(audio_bytes).decode("ascii"),
                    }
                })
            parts.append({"text": "TARGET (frames — same asset as audio above):"})
            for jpeg in media.frames_jpeg:
                parts.append({
                    "inline_data": {
                        "mime_type": "image/jpeg",
                        "data": base64.b64encode(jpeg).decode("ascii"),
                    }
                })
        parts.append({"text": "Classify this asset and return JSON per the schema."})

        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": 0.1,
                "topP": 0.9,
                "responseMimeType": "application/json",
                "responseSchema": RESPONSE_SCHEMA,
            },
            "safetySettings": [
                {"category": c, "threshold": "BLOCK_NONE"} for c in [
                    "HARM_CATEGORY_HARASSMENT",
                    "HARM_CATEGORY_HATE_SPEECH",
                    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    "HARM_CATEGORY_DANGEROUS_CONTENT",
                ]
            ],
        }

        url = f"{GEMINI_ENDPOINT}/{self.model}:generateContent"
        raw = self._post_with_retry(url, body)
        if "error" in raw:
            return ClassifierResult(classification={}, raw_response=raw,
                                    error=str(raw["error"]), model=self.model)

        try:
            text = raw["candidates"][0]["content"]["parts"][0]["text"]
            classification = json.loads(text)
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            return ClassifierResult(classification={}, raw_response=raw,
                                    error=f"parse_error: {e}", model=self.model)

        if folder_category and not classification.get("folder_category"):
            classification["folder_category"] = folder_category
        # Stamp classifier_version for governance/reproducibility.
        classification.setdefault("classifier_version", CLASSIFIER_VERSION)

        return ClassifierResult(
            classification=classification,
            raw_response=raw,
            model=self.model,
            usage=raw.get("usageMetadata", {}),
            classifier_version=CLASSIFIER_VERSION,
        )

    def adjudicate_shot(
        self,
        media: SampledMedia,
        current_shot: str,
        *,
        video_bytes: Optional[bytes] = None,
        reference_frames: Optional[List[tuple[bytes, str]]] = None,
    ) -> Optional[str]:
        """Second-pass Shot-only call. Called when the first-pass Shot is one
        of the ambiguous set (Mid/Wide/POV). Sends the same media with a
        focused prompt asking only for ShotType. Returns the adjudicated
        Shot token (may equal ``current_shot``) or None on failure."""
        if not media.frames_jpeg:
            return None

        prompt = f"""\
You are performing SECOND-PASS SHOT TYPE ADJUDICATION for the PawpyStuff
Naming Convention. The first pass produced ShotType={current_shot!r} for
this asset, but that ShotType is in the ambiguous set (Wide/Mid/POV) —
please re-evaluate ONLY the ShotType.

Approved ShotType tokens: {sorted(v.SHOT_TYPES)}

DECISION RULES (in order):
1. If the camera is CLEARLY a fixed dashboard/cabin mount with a stable
   horizon across all frames -> DashCam.
2. If the recorder's own hands/arms/feet are visible IN the frame from
   the recorder's own perspective, or body-mounted camera -> POV.
3. If a shoulder/head clearly acts as foreground framing toward another
   subject -> OTS.
4. If straight-down top-down view -> Overhead.
5. If camera at/below platform level with subject towering above -> LowAngle.
6. Otherwise pick the FRAMING that best fits:
   - Wide: substantial vehicle/environment context visible around subject
     (seat backs + doors + ceiling + other passengers). Use Wide when the
     backseat area and its surroundings are clearly visible.
   - Mid: subject dominates with only immediate context (e.g., subject +
     adjacent seat cushion, no ceiling/doors).
   - CU: subject occupies most of frame, minimal context.
   - ECU: extreme close-up on a subject detail.
7. WHEN TORN BETWEEN Mid and Wide on backseat clips: prefer Wide if you
   can see any door frame, ceiling, window, front seat back, or other
   passenger simultaneously with the primary subject.

Return JSON with a single key: {{"shot_type": "..."}}
"""
        header = [
            f"Asset duration: {media.probe.duration_sec:.1f}s. "
            f"Dimensions: {media.probe.width}x{media.probe.height}."
        ]

        parts: List[Dict[str, Any]] = [{"text": "\n".join(header)}]

        if reference_frames:
            parts.append({"text": "SHOT REFERENCE EXAMPLES:"})
            for ref_jpeg, ref_label in reference_frames:
                parts.append({"text": f"Reference: {ref_label}"})
                parts.append({
                    "inline_data": {
                        "mime_type": "image/jpeg",
                        "data": base64.b64encode(ref_jpeg).decode("ascii"),
                    }
                })

        use_video = (
            video_bytes is not None
            and 0 < media.probe.duration_sec <= VIDEO_INPUT_MAX_SEC
            and len(video_bytes) <= VIDEO_INPUT_MAX_BYTES
        )
        if use_video:
            parts.append({"text": "TARGET video:"})
            parts.append({
                "inline_data": {
                    "mime_type": media.probe.mime_type or "video/mp4",
                    "data": base64.b64encode(video_bytes).decode("ascii"),
                }
            })
        else:
            parts.append({"text": "TARGET frames:"})
            for jpeg in media.frames_jpeg:
                parts.append({
                    "inline_data": {
                        "mime_type": "image/jpeg",
                        "data": base64.b64encode(jpeg).decode("ascii"),
                    }
                })

        body = {
            "systemInstruction": {"parts": [{"text": prompt}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": 0.05,
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "object",
                    "properties": {
                        "shot_type": {"type": "string", "enum": sorted(v.SHOT_TYPES)},
                    },
                    "required": ["shot_type"],
                },
            },
        }
        url = f"{GEMINI_ENDPOINT}/{self.model}:generateContent"
        raw = self._post_with_retry(url, body)
        try:
            text = raw["candidates"][0]["content"]["parts"][0]["text"]
            j = json.loads(text)
            ans = j.get("shot_type")
            if ans in v.SHOT_TYPES:
                return ans
        except (KeyError, IndexError, json.JSONDecodeError, TypeError):
            pass
        return None

    def _post_with_retry(self, url: str, body: Dict[str, Any],
                         *, max_attempts: int = 4) -> Dict[str, Any]:
        delay = 2.0
        last: Dict[str, Any] = {}
        for attempt in range(1, max_attempts + 1):
            try:
                r = requests.post(url, params={"key": self.api_key}, json=body,
                                  timeout=self.timeout)
                if r.status_code == 200:
                    return r.json()
                if r.status_code in (429, 500, 502, 503, 504):
                    last = {"error": {"http": r.status_code, "body": r.text[:600]}}
                    time.sleep(delay)
                    delay = min(delay * 2, 30.0)
                    continue
                try:
                    return r.json()
                except ValueError:
                    return {"error": {"http": r.status_code, "body": r.text[:600]}}
            except requests.RequestException as e:
                last = {"error": {"exception": repr(e)}}
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
        return last or {"error": "unknown"}
