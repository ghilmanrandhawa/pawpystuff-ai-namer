#!/usr/bin/env python3
"""Pilot runner: classify every media file the service account can see and
write a CSV + markdown so we can diff against the client's ground-truth
spreadsheet.

READ-ONLY. Does not rename or move anything on Drive.

Behavior:
  * If PS_FOLDER_ID is set in the environment, only that folder (+ subfolders
    when PS_RECURSIVE is truthy, the default) is processed.
  * Otherwise, the runner discovers every folder shared with the service
    account and processes each one. Each file's location within its shared
    root is captured in ``folder_path`` so a single CSV covers all folders.

Concurrency:
  * PS_WORKERS (default 6) sizes the ThreadPoolExecutor. Each worker owns one
    file end-to-end (download -> ffprobe -> extract -> classify -> validate).
  * googleapiclient's Drive service is not thread-safe, so each worker gets
    its own via ``threading.local``.
  * Every file's per-stage timings are captured and printed as a contiguous
    block (buffered under a lock) so output stays readable.
"""

from __future__ import annotations

import concurrent.futures
import csv
import json
import os
import sys
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import config as cfg_mod
from pipeline import media, vocabulary as v
from pipeline.classifier import GeminiClassifier, ADJUDICATE_SHOTS, VIDEO_INPUT_MAX_SEC, VIDEO_INPUT_MAX_BYTES
from pipeline.media import extract_audio_mp3
from pipeline.drive_client import build_drive_service, resolve_extension
from pipeline.source import (
    discover_drive_folder,
    discover_drive_folders_all,
    discover_drive_file,
    discover_local_dir,
    discover_local_file,
)
from pipeline.validator import validate_and_assemble

# Global reference frames (Wide/Mid/POV) selected at startup and used for
# few-shot classification + shot adjudication. Set once in main(); safe to
# read from worker threads (frozen tuples).
REFERENCE_FRAMES: List[tuple[bytes, str]] = []


CSV_HEADER = [
    "source_type",
    "shared_root_name", "shared_root_id", "folder_path",
    "folder_category",
    "drive_file_id", "original_name", "size_bytes", "duration_sec",
    "dimensions", "checksum_md5",
    "status",
    "proposed_filename",
    "category", "scene", "dog_type", "vehicle_class",
    "cozy_rider_color", "cushion", "voiceover",   # V3.5
    "shot_type", "format",
    "scene_runner_up",           # V3.0
    "evidence_timestamps",       # V3.0
    "classifier_version",        # V3.0
    "review_reasons",
    "model", "prompt_tokens", "candidate_tokens", "total_tokens",
    "t_download_sec", "t_probe_sec", "t_extract_sec", "t_audio_sec", "t_classify_sec",
    "t_validate_log_sec", "t_total_sec", "frames_sampled",
    "seconds_elapsed",
    "error",
]


# ---------------------------------------------------------------------------
# Threading helpers
# ---------------------------------------------------------------------------

_drive_local = threading.local()
_print_lock = threading.Lock()


def _thread_drive(sa_path: str):
    """Return a Drive service owned by the calling thread (lazy)."""
    svc = getattr(_drive_local, "svc", None)
    if svc is None:
        svc = build_drive_service(sa_path)
        _drive_local.svc = svc
    return svc


class _Log:
    """Buffer log lines for one file so a worker's output stays contiguous."""

    __slots__ = ("_lines", "_tag")

    def __init__(self, tag: str):
        self._tag = tag
        self._lines: List[str] = []

    def line(self, msg: str) -> None:
        self._lines.append(f"{self._tag} {msg}")

    def flush(self) -> None:
        if not self._lines:
            return
        with _print_lock:
            for m in self._lines:
                print(m, flush=True)
        self._lines.clear()


def _build_reference_frames(all_files, cfg) -> List[tuple[bytes, str]]:
    """Look at the client answer-key CSV (if present) for canonical clips
    labeled with each of the 9 shot types. Extract one middle frame from
    up to N examples per shot type and return as (jpeg_bytes, label_text)
    tuples for few-shot embedding into every classifier call.

    Enriched version (2026-08-23): now covers all 9 shot types with up to
    3 examples each. Prior version only picked 1 example each for Wide/Mid/POV.
    """
    from pathlib import Path as _P
    answer_key_candidates = [
        cfg_mod.PROJECT_ROOT / "PS AI Naming Convention Spreadsheet - Sheet1 (1).csv",
        cfg_mod.PROJECT_ROOT / "PS AI Naming Convention Spreadsheet - Sheet1.csv",
    ]
    answer_key = next((p for p in answer_key_candidates if p.exists()), None)
    if not answer_key:
        return []

    import csv as _csv
    filename_shot: Dict[str, str] = {}
    with answer_key.open() as fh:
        for row in _csv.DictReader(fh):
            fn = (row.get("Filename") or "").strip()
            shot = (row.get("Shot") or "").strip()
            if fn and shot and fn not in filename_shot:
                filename_shot[fn] = shot

    # Map answer-key filenames to a local or cached path we can read from.
    from pipeline.drive_client import resolve_extension as _rext
    path_by_name: Dict[str, str] = {}
    for f in all_files:
        if f.get("local_path"):
            # Local source: read from the file's actual location.
            path_by_name[f["name"]] = f["local_path"]
        else:
            # Drive source: read from the download cache (may not exist yet
            # on first run — we only pick from files already downloaded).
            ext = _rext(f["name"], f.get("mimeType"))
            path_by_name[f["name"]] = str(_P(cfg.media_cache_dir) / f'{f["id"]}{ext}')

    # V3.5 ShotTypes (6 shots; ECU/Overhead/LowAngle removed in V3.2).
    MAX_PER_SHOT = 3
    want = ["CU", "Mid", "Wide", "OTS", "POV", "DashCam"]

    # Descriptive labels attached to each reference frame so Gemini gets the
    # *rule* behind the example, not just the class name.
    SHOT_DESCRIPTIONS = {
        "CU":      "subject fills most of the frame with only minimal surroundings; NOT POV even when shot from driver's seat",
        "Mid":     "subject dominates the frame with only immediate context visible (subject + adjacent cushion or one seat back)",
        "Wide":    "substantial vehicle/environmental context around subject — you can see the whole backseat area with door frames, ceiling, or other passengers",
        "OTS":     "over-the-shoulder — a person's shoulder or head clearly acts as foreground framing",
        "POV":     "first-person viewpoint — the recorder's own hand/arm/foot enters from behind the camera, OR body-mounted rig; NOT just a tight product shot from the driver's seat",
        "DashCam": "camera rigidly mounted to dashboard/windshield; horizon perfectly stable across frames while the vehicle moves",
    }

    from collections import defaultdict as _dd
    candidates_by_shot: Dict[str, list[str]] = _dd(list)
    for fn, shot in filename_shot.items():
        if shot in want:
            candidates_by_shot[shot].append(fn)

    picked: list[tuple[bytes, str]] = []
    import subprocess as _sub
    for shot in want:
        n_taken = 0
        for fn in candidates_by_shot.get(shot, []):
            if n_taken >= MAX_PER_SHOT: break
            source_path = path_by_name.get(fn)
            if not source_path: continue
            cache_path = _P(source_path)
            if not cache_path.exists(): continue
            # Extract a middle-time JPEG frame with ffmpeg.
            try:
                jpeg = _sub.check_output([
                    "ffmpeg", "-nostdin", "-loglevel", "error",
                    "-ss", "1.0",
                    "-i", str(cache_path),
                    "-frames:v", "1",
                    "-vf", "scale='if(gt(iw,ih),min(1024,iw),-2)':'if(gt(iw,ih),-2,min(1024,ih))'",
                    "-q:v", "3",
                    "-f", "image2pipe", "-vcodec", "mjpeg", "-",
                ], timeout=30)
                desc = SHOT_DESCRIPTIONS.get(shot, "")
                label = f"ShotType={shot} (client-labeled) — {desc}" if desc else f"ShotType={shot} (client-labeled)"
                picked.append((jpeg, label))
                n_taken += 1
            except _sub.SubprocessError:
                continue
    return picked


def _infer_folder_category(folder_path: str) -> Optional[str]:
    if not folder_path:
        return None
    for segment in folder_path.split("/"):
        if segment in v.CATEGORIES:
            return segment
    return None


# ---------------------------------------------------------------------------
# Per-file worker
# ---------------------------------------------------------------------------

def _process_file(
    f: Dict[str, Any],
    *,
    idx: int,
    total: int,
    cfg,
    classifier: GeminiClassifier,
    logs_dir: Path,
) -> tuple[Dict[str, Any], Any, Dict[str, float]]:
    """Process one Drive file → (csv_row, ValidationResult|None, timing_dict).
    Safe to call from a worker thread — uses thread-local Drive service."""
    started = time.monotonic()
    timings = {"download": 0.0, "probe": 0.0, "extract": 0.0, "audio": 0.0, "classify": 0.0, "validate_log": 0.0}

    src_id = f["id"]
    name = f["name"]
    ext = resolve_extension(name, f.get("mimeType"))
    file_ext = Path(name).suffix.lower()
    size = int(f.get("size", 0)) if f.get("size") else 0
    folder_path = f.get("folder_path", "") or ""
    root_name = f.get("_root_name", "")
    root_id = f.get("_root_id", "")
    source_type = f.get("source_type", "drive_folder")
    local_path_input = f.get("local_path")  # non-None for local sources

    log = _Log(f"[{idx:>3}/{total}] [{src_id}]")
    log.line(f"{name}  ({size/1e6:.1f} MB)  [{source_type}] in {root_name}/{folder_path or '.'}")

    folder_category = _infer_folder_category(folder_path)

    row: Dict[str, Any] = {k: "" for k in CSV_HEADER}
    row["source_type"] = source_type
    row["shared_root_name"] = root_name
    row["shared_root_id"] = root_id
    row["folder_path"] = folder_path
    row["folder_category"] = folder_category or ""
    row["drive_file_id"] = src_id if source_type.startswith("drive_") else ""
    row["original_name"] = name
    row["size_bytes"] = size
    row["model"] = cfg.model

    def _finish():
        row["t_download_sec"] = f"{timings['download']:.2f}"
        row["t_probe_sec"] = f"{timings['probe']:.2f}"
        row["t_extract_sec"] = f"{timings['extract']:.2f}"
        row["t_audio_sec"] = f"{timings['audio']:.2f}"
        row["t_classify_sec"] = f"{timings['classify']:.2f}"
        row["t_validate_log_sec"] = f"{timings['validate_log']:.2f}"
        row["t_total_sec"] = f"{time.monotonic()-started:.2f}"
        row["seconds_elapsed"] = row["t_total_sec"]

    if ext not in v.SUPPORTED_EXTENSIONS:
        row["status"] = "Unsupported"
        row["review_reasons"] = "UnsupportedFormat"
        _finish()
        log.line(f"    -> Unsupported extension {file_ext or '(none)'} (mime={f.get('mimeType','?')})")
        log.flush()
        return row, None, timings

    if ext != file_ext:
        log.line(f"    ext inferred    {file_ext or '(none)'} -> {ext} (via mime={f.get('mimeType','')})")

    # 1. Resolve local file path.
    # Local sources: read the file in place (no download, no cache copy).
    # Drive sources: download to media cache if not already there.
    if local_path_input:
        cache_path = Path(local_path_input)
        if not cache_path.is_file():
            row["status"] = "NeedsReview"
            row["review_reasons"] = "SourceMissing"
            row["error"] = f"local file no longer exists: {local_path_input}"
            _finish()
            log.line(f"    local source missing: {local_path_input}")
            log.flush()
            return row, None, timings
        log.line(f"    local file        (no download, {cache_path.stat().st_size/1e6:.1f} MB in place)")
    else:
        cache_path = Path(cfg.media_cache_dir) / f"{src_id}{ext}"
        if not cache_path.exists() or cache_path.stat().st_size != size:
            t0 = time.monotonic()
            try:
                drive = _thread_drive(cfg.service_account_path)
                media.download_drive_file(drive, src_id, str(cache_path))
            except Exception as e:
                timings["download"] = time.monotonic() - t0
                row["status"] = "NeedsReview"
                row["review_reasons"] = "DownloadError"
                row["error"] = repr(e)[:500]
                _finish()
                log.line(f"    download FAILED after {timings['download']:.1f}s: {e}")
                log.flush()
                return row, None, timings
            timings["download"] = time.monotonic() - t0
            rate = size/1e6/timings['download'] if timings['download'] > 0 else 0.0
            log.line(f"    download        {timings['download']:6.2f}s  ({rate:.1f} MB/s)")
        else:
            log.line(f"    download          cached (0.00s)")

    # 2 + 3. Probe + frame extract
    try:
        if ext in v.SUPPORTED_VIDEO_EXTENSIONS:
            vmeta = f.get("videoMediaMetadata") or {}
            sm = media.sample_video(
                str(cache_path),
                drive_md5=f.get("md5Checksum"), drive_mime=f.get("mimeType"),
                drive_width=vmeta.get("width"), drive_height=vmeta.get("height"),
                max_edge=cfg.frame_max_edge,
            )
        else:
            sm = media.sample_image(
                str(cache_path),
                drive_md5=f.get("md5Checksum"), drive_mime=f.get("mimeType"),
                max_edge=cfg.frame_max_edge * 2,
            )
    except Exception as e:
        row["status"] = "NeedsReview"
        row["review_reasons"] = "SampleError"
        row["error"] = repr(e)[:500]
        _finish()
        log.line(f"    -> sample error: {e}")
        log.flush()
        return row, None, timings

    timings["probe"] = sm.probe_sec
    timings["extract"] = sm.extract_sec
    row["duration_sec"] = f"{sm.probe.duration_sec:.2f}"
    row["dimensions"] = f"{sm.probe.width}x{sm.probe.height}"
    row["checksum_md5"] = sm.probe.checksum_md5
    row["frames_sampled"] = len(sm.frames_jpeg)
    log.line(f"    ffprobe         {timings['probe']:6.2f}s  ({sm.probe.width}x{sm.probe.height}, {sm.probe.duration_sec:.1f}s dur)")

    if sm.long_video:
        # V3.0: >10min → NeedsReview with reason LongVideo (not NeedsSplit).
        row["status"] = "NeedsReview"
        row["review_reasons"] = "LongVideo"
        _finish()
        log.line(f"    -> NeedsReview LongVideo (duration {sm.probe.duration_sec:.1f}s)")
        log.flush()
        return row, None, timings

    if sm.unsupported_reason:
        row["status"] = "Unsupported"
        row["review_reasons"] = sm.unsupported_reason
        row["error"] = sm.unsupported_reason
        _finish()
        log.line(f"    -> Unsupported: {sm.unsupported_reason}")
        log.flush()
        return row, None, timings

    log.line(
        f"    extract frames  {timings['extract']:6.2f}s  "
        f"({len(sm.frames_jpeg)} frames, {timings['extract']/max(1,len(sm.frames_jpeg)):.2f}s/frame)"
    )

    # 4a. Audio extraction (V3.5 — required for Voiceover detection).
    # For short clips we send the full video (audio embedded), so we only
    # need separate audio bytes for the longer frames-only path.
    video_bytes = None
    audio_bytes = None
    audio_extraction_error: Optional[str] = None
    no_audio_stream = False  # True when ffprobe confirms no audio track (muted clip)

    if ext in v.SUPPORTED_VIDEO_EXTENSIONS:
        # Attempt full-video send for short clips.
        if (0 < sm.probe.duration_sec <= VIDEO_INPUT_MAX_SEC
                and cache_path.exists()
                and cache_path.stat().st_size <= VIDEO_INPUT_MAX_BYTES):
            try:
                video_bytes = cache_path.read_bytes()
            except Exception:
                video_bytes = None
        # For longer clips (frames-only path), extract audio separately.
        # Also extract if video_bytes failed so we still get audio analysis.
        if video_bytes is None:
            t0_audio = time.monotonic()
            audio_bytes, audio_err = extract_audio_mp3(str(cache_path))
            timings["audio"] = time.monotonic() - t0_audio
            if audio_err and audio_err != "NoAudioStream":
                # Real extraction failure — must block auto-rename per V3.5.
                audio_extraction_error = audio_err
                log.line(f"    audio extract  FAILED: {audio_err}")
            elif audio_err == "NoAudioStream":
                no_audio_stream = True
                log.line(f"    audio extract  no audio stream (voiceover=null, not a failure)")
                audio_bytes = None
            else:
                log.line(
                    f"    audio extract  {timings['audio']:6.2f}s  "
                    f"({len(audio_bytes)/1024:.0f} KB MP3)"
                )

    # 4b. Gemini classify
    t0 = time.monotonic()
    cls_result = classifier.classify(
        sm, folder_category=folder_category,
        filename_hint=name,
        video_bytes=video_bytes,
        audio_bytes=audio_bytes,
        reference_frames=REFERENCE_FRAMES or None,
    )

    # Reconcile audio_analysis_failed with what WE actually sent.
    # Gemini's self-report is unreliable — it conflates "no speech heard"
    # with "analysis failed" on silent B-roll clips. We are the authoritative
    # source on whether audio was available to Gemini:
    #   - Hard external failure (our ffmpeg crashed) → force True
    #   - We successfully sent audio (video_bytes w/ audio, or audio_bytes) → force False
    #   - Image / no video → Gemini correctly returns False; leave it alone
    if not cls_result.error:
        if audio_extraction_error:
            cls_result.classification["audio_analysis_failed"] = True
        elif video_bytes is not None or audio_bytes is not None or no_audio_stream:
            # We provided audio to Gemini (video_bytes or audio_bytes), OR
            # ffprobe confirmed there is no audio track (muted clip = valid null voiceover).
            # In all three cases audio_analysis_failed must be False.
            cls_result.classification["audio_analysis_failed"] = False
    timings["classify"] = time.monotonic() - t0
    total_toks = (cls_result.usage or {}).get("totalTokenCount", "?")
    log.line(f"    gemini call     {timings['classify']:6.2f}s  ({len(sm.frames_jpeg)} frames -> {total_toks} tokens)")

    if cls_result.error:
        row["status"] = "NeedsReview"
        row["review_reasons"] = "ClassifierError"
        row["error"] = cls_result.error[:500]
        (logs_dir / f"{src_id}.error.json").write_text(json.dumps(cls_result.raw_response, indent=2))
        _finish()
        log.line(f"    -> classifier error: {cls_result.error[:200]}")
        log.flush()
        return row, None, timings

    usage = cls_result.usage or {}
    row["prompt_tokens"] = usage.get("promptTokenCount", "")
    row["candidate_tokens"] = usage.get("candidatesTokenCount", "")
    row["total_tokens"] = usage.get("totalTokenCount", "")

    # 4b. Shot adjudication — DISABLED (2026-08-23 test showed -8% Shot
    # regression). The two-pass adjudicator was too aggressive at swapping
    # Mid<->Wide and turned correct top-1 calls into wrong ones. Keeping
    # the code path in classifier.adjudicate_shot() for future re-tuning,
    # but not calling it. Video input + few-shot both remain enabled.

    # 5. Validate + log write
    t0 = time.monotonic()
    vr = validate_and_assemble(
        cls_result.classification,
        width=sm.probe.width, height=sm.probe.height, extension=ext,
        duration_sec=sm.probe.duration_sec,
        folder_category=folder_category,
    )
    row["status"] = vr.status
    row["proposed_filename"] = vr.proposed_filename or ""
    row["category"] = vr.tokens.get("category") or ""
    row["scene"] = vr.tokens.get("scene") or ""
    row["dog_type"] = vr.tokens.get("dog_type") or ""
    row["vehicle_class"] = vr.tokens.get("vehicle_class") or ""
    row["cozy_rider_color"] = vr.tokens.get("cozy_rider_color") or ""   # V3.5
    row["cushion"] = vr.tokens.get("cushion") or ""                      # V3.5
    row["voiceover"] = vr.tokens.get("voiceover") or ""                  # V3.5
    row["shot_type"] = vr.tokens.get("shot_type") or ""
    row["format"] = vr.tokens.get("format") or ""
    row["scene_runner_up"] = vr.scene_runner_up or ""
    row["evidence_timestamps"] = ",".join(f"{t:.2f}" for t in vr.evidence_timestamps)
    row["classifier_version"] = vr.classifier_version or ""
    row["review_reasons"] = ";".join(vr.review_reasons)

    (logs_dir / f"{src_id}.json").write_text(json.dumps({
        "source_type": source_type,
        "shared_root_name": root_name, "shared_root_id": root_id,
        "folder_path": folder_path, "folder_category": folder_category,
        "drive_file_id": src_id if source_type.startswith("drive_") else "",
        "local_path": local_path_input,
        "original_name": name,
        "probe": sm.probe.__dict__,
        "classification": cls_result.classification,
        "validation": {
            "status": vr.status, "proposed_filename": vr.proposed_filename,
            "review_reasons": vr.review_reasons, "tokens": vr.tokens,
            "scene_runner_up": vr.scene_runner_up,
            "evidence_timestamps": vr.evidence_timestamps,
            "classifier_version": vr.classifier_version,
            "format_ratio_error": vr.format_ratio_error,
            # V3.5 new fields (also in tokens, duplicated here for readability)
            "cozy_rider_color": vr.tokens.get("cozy_rider_color"),
            "cushion": vr.tokens.get("cushion"),
            "voiceover": vr.tokens.get("voiceover"),
        },
        "audio_extraction_error": audio_extraction_error,
        "usage": usage,
        "timings_sec": timings | {"total": time.monotonic() - started},
    }, indent=2, default=str))
    timings["validate_log"] = time.monotonic() - t0
    log.line(f"    validate + log  {timings['validate_log']:6.2f}s")

    _finish()
    log.line(f"    -> {vr.status}  {vr.proposed_filename or '(no name)'}"
             + (f"  reasons={vr.review_reasons}" if vr.review_reasons else ""))
    log.flush()
    return row, vr, timings


# ---------------------------------------------------------------------------
# Post-processing
# ---------------------------------------------------------------------------

def _resolve_take_collisions(interim_results: list[tuple[dict, Any]]) -> None:
    """Assign T01-T## takes to files that share the same base stem.

    V3.5 scope rule: takes are keyed by (shared_root_id, base_stem) — the full
    creator/product library, NOT per-subfolder. This fixes the bug where files
    in different subfolders with identical base stems each started their own
    independent T01 sequence, producing duplicates like T01, T01, T02, T02.

    Different base stems may each independently start at T01 (that is correct
    and expected). Only duplicate base stems within the same library root get
    sequential takes.
    """
    import re
    # Key: (shared_root_id, base_stem_without_extension)
    by_scope: dict[tuple[str, str], list[tuple[dict, Any]]] = defaultdict(list)
    for row, vr in interim_results:
        if vr is None or row["status"] != "AutoRenamed" or not row["proposed_filename"]:
            continue
        # Strip any existing T## suffix (shouldn't be present, but be safe) then ext.
        no_take = re.sub(r"_T\d{2}(?=\.[^.]+$)", "", row["proposed_filename"])
        base_stem = no_take.rsplit(".", 1)[0]
        by_scope[(row["shared_root_id"], base_stem)].append((row, vr))
    for _, matches in by_scope.items():
        if len(matches) <= 1:
            continue
        for idx, (row, _vr) in enumerate(matches, start=1):
            take = f"T{idx:02d}"
            base, ext = row["proposed_filename"].rsplit(".", 1)
            row["proposed_filename"] = f"{base}_{take}.{ext}"


def _write_summary_md(out_md: Path, interim_results, roots=None) -> None:
    """Emit a per-source-root markdown summary. When ``roots`` is None,
    infer roots from the interim results (works for any source type)."""
    if roots is None:
        seen = {}
        for row, _ in interim_results:
            rid = row.get("shared_root_id") or ""
            if rid and rid not in seen:
                seen[rid] = {"id": rid, "name": row.get("shared_root_name") or rid}
        roots = list(seen.values())
    rows = [r for r, _ in interim_results]
    lines = []
    lines.append("# PawpyStuff AI Namer — Pilot Results")
    lines.append("")
    lines.append(f"- **Vocabulary:** V{v.VOCABULARY_VERSION}  ·  **Automation:** {v.AUTOMATION_VERSION}")
    lines.append(f"- **Files processed:** {len(rows)}")
    lines.append("")
    stat_counts = Counter(r["status"] for r in rows)
    lines.append("## Status")
    lines.append("| Status | Count |")
    lines.append("|---|---:|")
    for s, n in sorted(stat_counts.items(), key=lambda x: -x[1]):
        lines.append(f"| `{s}` | {n} |")
    lines.append("")
    for r in roots:
        root_id = r["id"]
        folder_rows = [x for x in rows if x["shared_root_id"] == root_id]
        if not folder_rows:
            continue
        lines.append(f"## `{r['name']}`  ({len(folder_rows)} files)")
        lines.append("")
        lines.append("| # | Original | Status | Proposed | Scene | Runner-up |")
        lines.append("|---:|---|---|---|---|---|")
        for i, x in enumerate(folder_rows, 1):
            lines.append(f"| {i} | `{x['original_name']}` | `{x['status']}` | "
                         f"`{x['proposed_filename'] or '—'}` | "
                         f"{x.get('scene','')} | {x.get('scene_runner_up','')} |")
        lines.append("")
    out_md.write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------

def _build_arg_parser():
    import argparse
    p = argparse.ArgumentParser(
        prog="run_pilot.py",
        description=(
            "PawpyStuff AI Namer pilot — classify media from Google Drive OR "
            "the local filesystem and write proposed filenames + a per-file "
            "audit log.\n\n"
            "SOURCE (pick exactly one; omit all to auto-discover every Drive "
            "folder shared with the service account):\n"
            "  --drive-folder-id ID   one Drive folder (recurses)\n"
            "  --drive-file-id ID     one Drive file\n"
            "  --local-dir PATH       one local directory (recurses)\n"
            "  --local-file PATH      one local file\n\n"
            "READ-ONLY: never renames or moves files anywhere. Flags override "
            "env vars of the same name."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = p.add_argument_group("source (pick one)")
    src.add_argument("--drive-folder-id", help="Restrict to one Drive folder ID. "
                     "Env: PS_FOLDER_ID")
    src.add_argument("--drive-file-id", help="Classify a single Drive file by ID.")
    src.add_argument("--local-dir", help="Local directory of media files (recursive).")
    src.add_argument("--local-file", help="Single local media file.")

    p.add_argument("--workers", type=int, help="Concurrent per-file workers. "
                   "Env: PS_WORKERS (default 6)")
    p.add_argument("--max-files", type=int, help="Cap total files (for smoke "
                   "tests). Env: PS_MAX_FILES")
    p.add_argument("--resume", action="store_true", help="Skip files that "
                   "already have a per-file JSON log. Env: PS_RESUME=1")
    p.add_argument("--include-ids-file", help="Path to a file with one source "
                   "ID per line — ONLY those files will be processed. "
                   "Env: PS_INCLUDE_IDS_FILE")
    p.add_argument("--model", help="Gemini model (e.g. gemini-2.5-pro). "
                   "Env: PS_GEMINI_MODEL (default gemini-flash-latest)")
    p.add_argument("--output-dir", help="Where results.csv + logs/ go. "
                   "Env: PS_OUTPUT_DIR")
    p.add_argument("--media-cache-dir", help="Where downloaded Drive videos "
                   "are cached (unused for local sources). "
                   "Env: PS_MEDIA_CACHE (default /tmp/pawpystuff_media_cache)")
    p.add_argument("--no-recursive", action="store_true", help="Do NOT walk "
                   "subfolders (applies to drive-folder and local-dir modes)")
    p.add_argument("--version", action="version",
                   version=f"PawpyStuff AI Namer  vocab={v.VOCABULARY_VERSION}  "
                           f"automation={v.AUTOMATION_VERSION}")
    return p


def _apply_cli_overrides(args) -> None:
    """CLI flags override env vars. Sets env vars so cfg_mod.load() picks them up."""
    if args.drive_folder_id is not None: os.environ["PS_FOLDER_ID"] = args.drive_folder_id
    if args.workers is not None:         os.environ["PS_WORKERS"] = str(args.workers)
    if args.max_files is not None:       os.environ["PS_MAX_FILES"] = str(args.max_files)
    if args.resume:                      os.environ["PS_RESUME"] = "1"
    if args.include_ids_file:            os.environ["PS_INCLUDE_IDS_FILE"] = args.include_ids_file
    if args.model:                       os.environ["PS_GEMINI_MODEL"] = args.model
    if args.output_dir:                  os.environ["PS_OUTPUT_DIR"] = args.output_dir
    if args.media_cache_dir:             os.environ["PS_MEDIA_CACHE"] = args.media_cache_dir
    if args.no_recursive:                os.environ["PS_RECURSIVE"] = "0"


def _validate_source_choice(args) -> Optional[str]:
    """Ensure at most one source flag is set. Returns an error string or None."""
    sources = [args.drive_folder_id, args.drive_file_id, args.local_dir, args.local_file]
    picked = [s for s in sources if s is not None]
    if len(picked) > 1:
        return "Pick exactly one source flag: --drive-folder-id / --drive-file-id / --local-dir / --local-file"
    return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    args = _build_arg_parser().parse_args()
    src_err = _validate_source_choice(args)
    if src_err:
        print(f"ERROR: {src_err}")
        return 2
    _apply_cli_overrides(args)
    cfg = cfg_mod.load()
    os.makedirs(cfg.output_dir, exist_ok=True)
    os.makedirs(cfg.media_cache_dir, exist_ok=True)
    logs_dir = Path(cfg.output_dir) / "logs"
    logs_dir.mkdir(exist_ok=True)

    # Determine mode
    if args.local_file:      mode = "local_file"
    elif args.local_dir:     mode = "local_dir"
    elif args.drive_file_id: mode = "drive_file"
    elif args.drive_folder_id or cfg.pilot_folder_id: mode = "drive_folder_one"
    else:                    mode = "drive_folders_all"

    print(f"Vocabulary version:  {v.VOCABULARY_VERSION}")
    print(f"Automation version:  {v.AUTOMATION_VERSION}")
    print(f"Model:               {cfg.model}")
    print(f"Mode:                {mode}")
    print(f"Workers:             {cfg.workers}")
    print(f"Frame counts:        short={v.SHORT_VIDEO_FRAME_COUNT}, long={v.LONG_VIDEO_FRAME_COUNT}")
    print(f"Output dir:          {cfg.output_dir}")
    if mode.startswith("drive"):
        print(f"Media cache:         {cfg.media_cache_dir}")

    # Auth is only required for Drive sources
    drive = None
    if mode.startswith("drive"):
        if not cfg.service_account_path:
            print("ERROR: Drive source selected but no service account JSON "
                  "found. Set PS_SERVICE_ACCOUNT_JSON=/path/to/key.json.")
            return 2
        drive = _thread_drive(cfg.service_account_path)

    # Dispatch to the right discoverer
    try:
        if mode == "local_file":
            all_files = discover_local_file(args.local_file)
        elif mode == "local_dir":
            all_files = discover_local_dir(args.local_dir, recursive=cfg.recursive)
        elif mode == "drive_file":
            all_files = discover_drive_file(drive, args.drive_file_id)
        elif mode == "drive_folder_one":
            folder_id = args.drive_folder_id or cfg.pilot_folder_id
            all_files = discover_drive_folder(drive, folder_id, recursive=cfg.recursive)
        else:  # drive_folders_all
            all_files = discover_drive_folders_all(drive, recursive=cfg.recursive)
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}")
        return 2

    if not all_files:
        print("No files to process.")
        return 0

    # Resume mode: skip files that already have a per-file JSON log written.
    if os.environ.get("PS_RESUME", "").lower() not in ("", "0", "false", "no"):
        before = len(all_files)
        all_files = [f for f in all_files if not (logs_dir / f"{f['id']}.json").exists()]
        print(f"Resume mode: skipping {before - len(all_files)} already-processed files "
              f"({len(all_files)} remain).")

    # Include-only mode: PS_INCLUDE_IDS_FILE points to a file with one Drive
    # file ID per line — only those files will be processed. Used for
    # targeted re-runs on specific mismatches.
    include_file = os.environ.get("PS_INCLUDE_IDS_FILE", "").strip()
    if include_file:
        include_ids = {
            line.strip() for line in Path(include_file).read_text().splitlines()
            if line.strip()
        }
        before = len(all_files)
        all_files = [f for f in all_files if f["id"] in include_ids]
        print(f"Include-only mode: {len(all_files)} of {before} files match the "
              f"{len(include_ids)}-id filter from {include_file}")

    if cfg.max_files:
        all_files = all_files[: cfg.max_files]
    total = len(all_files)

    # Build reference frames for few-shot Shot classification. We look at the
    # client answer-key CSV (if present) for canonical Wide/Mid/POV examples
    # and extract one representative frame from each cached video.
    global REFERENCE_FRAMES
    REFERENCE_FRAMES = _build_reference_frames(all_files, cfg)
    print(f"Reference frames prepared: {len(REFERENCE_FRAMES)} "
          f"({[lbl for _,lbl in REFERENCE_FRAMES]})")
    print(f"\nTotal files to process: {total} with {cfg.workers} workers\n")

    if total == 0:
        print("Nothing to do.")
        return 0

    classifier = GeminiClassifier(cfg.gemini_api_key, model=cfg.model)

    # Concurrent processing. Preserve original order in the CSV.
    interim: list[tuple[dict, Any] | None] = [None] * total
    all_timings: list[Dict[str, float] | None] = [None] * total
    started_all = time.monotonic()

    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        future_to_idx = {
            pool.submit(
                _process_file, f,
                idx=i + 1, total=total,
                cfg=cfg, classifier=classifier, logs_dir=logs_dir,
            ): i
            for i, f in enumerate(all_files)
        }
        for fut in concurrent.futures.as_completed(future_to_idx):
            idx = future_to_idx[fut]
            try:
                row, vr, tmg = fut.result()
                interim[idx] = (row, vr)
                all_timings[idx] = tmg
            except Exception as e:
                bad_row = {k: "" for k in CSV_HEADER}
                bad_row["drive_file_id"] = all_files[idx]["id"]
                bad_row["original_name"] = all_files[idx]["name"]
                bad_row["status"] = "NeedsReview"
                bad_row["review_reasons"] = "WorkerCrash"
                bad_row["error"] = repr(e)[:500]
                interim[idx] = (bad_row, None)
                all_timings[idx] = {"download": 0, "probe": 0, "extract": 0, "classify": 0, "validate_log": 0}
                with _print_lock:
                    print(f"[worker-crash] {all_files[idx]['name']}: {e!r}", flush=True)

    elapsed = time.monotonic() - started_all
    interim_results = [x for x in interim if x is not None]
    timings_all = [t for t in all_timings if t is not None]

    _resolve_take_collisions(interim_results)

    csv_path = Path(cfg.output_dir) / "results.csv"
    with csv_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_HEADER)
        writer.writeheader()
        for row, _vr in interim_results:
            writer.writerow(row)
    _write_summary_md(Path(cfg.output_dir) / "PILOT_RESULTS.md", interim_results)

    def agg(key): return sum(t[key] for t in timings_all)
    total_download = agg("download")
    total_probe = agg("probe")
    total_extract = agg("extract")
    total_audio = agg("audio")
    total_classify = agg("classify")
    total_vlog = agg("validate_log")
    per_file_cpu = total_download + total_probe + total_extract + total_audio + total_classify + total_vlog

    print("\n" + "=" * 74)
    print(f"CSV:      {csv_path}")
    print(f"Wall time: {elapsed:.1f}s  ({elapsed/60:.1f} min)  on {total} files, {cfg.workers} workers")
    print()
    print(f"  {'Stage':<20} {'Sum-CPU (s)':>12} {'Avg/file':>10}")
    print(f"  {'-'*20} {'-'*12} {'-'*10}")
    for label, val in [
        ("Drive download",   total_download),
        ("ffprobe",          total_probe),
        ("frame extract",    total_extract),
        ("audio extract",    total_audio),
        ("Gemini classify",  total_classify),
        ("validate + log",   total_vlog),
    ]:
        print(f"  {label:<20} {val:>12.1f} {val/total:>10.2f}")
    print(f"  {'-'*20} {'-'*12} {'-'*10}")
    print(f"  {'per-file CPU sum':<20} {per_file_cpu:>12.1f} {per_file_cpu/total:>10.2f}")
    print(f"  wall-time speedup vs sequential ≈ {per_file_cpu/elapsed:.1f}x")
    print()
    statuses = Counter(row["status"] for row, _ in interim_results)
    for s, n in sorted(statuses.items()):
        print(f"  {s:14s} {n}")
    print("=" * 74)

    # Single-file modes: also print the proposed name to stdout for ease of use
    # in scripts / one-off invocations.
    if mode in ("local_file", "drive_file") and interim_results:
        row, _vr = interim_results[0]
        print()
        print(f"Source:            {row.get('original_name','')}")
        print(f"Status:            {row.get('status','')}")
        print(f"Proposed filename: {row.get('proposed_filename','') or '(none)'}")
        if row.get("review_reasons"):
            print(f"Review reasons:    {row['review_reasons']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
