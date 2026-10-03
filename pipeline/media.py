"""Drive download + frame sampling per PawpyStuff Naming Convention V3.0.

Frame sampling (per ``vocabulary.SHORT_VIDEO_FRAME_COUNT`` / ``LONG_VIDEO_FRAME_COUNT``):
  * ``<= SHORT_VIDEO_MAX_SEC`` (30s)   -> SHORT_VIDEO_FRAME_COUNT frames (currently 6)
  * ``30s .. LONG_VIDEO_THRESHOLD_SEC`` (10min) -> LONG_VIDEO_FRAME_COUNT frames (currently 20)
  * ``> LONG_VIDEO_THRESHOLD_SEC``     -> ``SampledMedia.long_video=True`` (validator routes to
                                        NeedsReview with reason ``LongVideo`` per V3.0)

Images pass through as a single frame. HEIC gets transcoded to JPEG on the
fly via ffmpeg so the vision model can read it while the original stays
untouched on disk.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from googleapiclient.http import MediaIoBaseDownload

from . import vocabulary as v


@dataclass
class MediaProbe:
    width: int
    height: int
    duration_sec: float          # 0 for images
    mime_type: str
    extension: str               # lowercase, includes leading dot
    checksum_md5: str            # from Drive metadata (falls back to computed)
    size_bytes: int


@dataclass
class SampledMedia:
    probe: MediaProbe
    frames_jpeg: List[bytes]     # each entry is a JPEG-encoded frame
    frame_timestamps_sec: List[float]
    long_video: bool = False     # True when duration > LONG_VIDEO_THRESHOLD_SEC
    unsupported_reason: Optional[str] = None
    # Instrumentation — set by sample_video/sample_image so the caller can
    # attribute wall time to probe vs frame-extract phases.
    probe_sec: float = 0.0
    extract_sec: float = 0.0

    # Backwards-compat alias for callers that still reference the pre-V3.0
    # field name. Safe to remove once all callers are updated.
    @property
    def needs_split(self) -> bool:  # pragma: no cover
        return self.long_video


# ---------------------------------------------------------------------------
# Drive download
# ---------------------------------------------------------------------------

def download_drive_file(drive_service, file_id: str, dest_path: str) -> str:
    """Stream a Drive file to dest_path. Returns dest_path on success."""
    req = drive_service.files().get_media(fileId=file_id, supportsAllDrives=True)
    with open(dest_path, "wb") as fh:
        dl = MediaIoBaseDownload(fh, req, chunksize=8 * 1024 * 1024)
        done = False
        while not done:
            _, done = dl.next_chunk()
    return dest_path


# ---------------------------------------------------------------------------
# ffprobe / ffmpeg helpers
# ---------------------------------------------------------------------------

def _ffprobe_video(path: str) -> tuple[int, int, float]:
    """Return (display_width, display_height, duration_sec) for a video file.

    Uses -show_streams so the Display Matrix side_data (with its rotation) is
    included — narrow -show_entries queries drop the rotation sub-field. iPhone
    clips are stored 3840x2160 with rotation=-90 and display as 2160x3840.
    """
    import json as _json
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-select_streams", "v:0",
        "-show_streams", "-show_format",
        path,
    ]
    j = _json.loads(subprocess.check_output(cmd, text=True))
    stream = (j.get("streams") or [{}])[0]
    w = int(stream.get("width") or 0)
    h = int(stream.get("height") or 0)
    dur = float((j.get("format") or {}).get("duration") or stream.get("duration") or 0.0)

    rot = 0
    for sd in stream.get("side_data_list") or []:
        r = sd.get("rotation")
        if r is not None:
            try:
                rot = int(r)
            except (TypeError, ValueError):
                rot = 0
            break
    if not rot:
        tag_rot = (stream.get("tags") or {}).get("rotate")
        if tag_rot is not None:
            try:
                rot = int(tag_rot)
            except (TypeError, ValueError):
                rot = 0
    if abs(rot) % 180 == 90:
        w, h = h, w
    return w, h, dur


def _md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_frame_jpeg(video_path: str, ts_sec: float, max_edge: int = 1024) -> bytes:
    """Grab a single JPEG frame at ts_sec, downscaled so the long edge <= max_edge."""
    # -ss BEFORE -i does fast seek (good enough for sampling).
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-ss", f"{max(0.0, ts_sec):.3f}",
        "-i", video_path,
        "-frames:v", "1",
        "-vf", f"scale='if(gt(iw,ih),min({max_edge},iw),-2)':'if(gt(iw,ih),-2,min({max_edge},ih))'",
        "-q:v", "3",
        "-f", "image2pipe", "-vcodec", "mjpeg", "-",
    ]
    return subprocess.check_output(cmd)


def _evenly_spaced_timestamps(duration_sec: float, count: int) -> List[float]:
    """Start, end, and (count-2) evenly spaced points strictly inside."""
    if count < 2 or duration_sec <= 0:
        return [0.0]
    # Nudge endpoints slightly inside so seek doesn't land past EOF.
    start = 0.0
    end = max(0.0, duration_sec - 0.05)
    if count == 2:
        return [start, end]
    step = (end - start) / (count - 1)
    return [round(start + step * i, 3) for i in range(count)]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def sample_video(
    local_path: str,
    *,
    drive_md5: Optional[str] = None,
    drive_mime: Optional[str] = None,
    drive_width: Optional[int] = None,
    drive_height: Optional[int] = None,
    max_edge: int = 1024,
) -> SampledMedia:
    """Probe + frame-sample a video per SOP rules.

    When Drive supplies display-oriented videoMediaMetadata dimensions, prefer
    those over ffprobe (they already account for rotation).
    """
    ext = Path(local_path).suffix.lower()
    if ext not in v.SUPPORTED_VIDEO_EXTENSIONS:
        return SampledMedia(
            probe=MediaProbe(0, 0, 0.0, drive_mime or "", ext, drive_md5 or "", os.path.getsize(local_path)),
            frames_jpeg=[], frame_timestamps_sec=[],
            unsupported_reason="UnsupportedFormat",
        )

    import time as _t
    _t0 = _t.monotonic()
    try:
        w, h, dur = _ffprobe_video(local_path)
    except (subprocess.CalledProcessError, ValueError, IndexError) as e:
        return SampledMedia(
            probe=MediaProbe(0, 0, 0.0, drive_mime or "", ext, drive_md5 or _md5(local_path), os.path.getsize(local_path)),
            frames_jpeg=[], frame_timestamps_sec=[],
            unsupported_reason=f"CorruptFile: {e}",
            probe_sec=_t.monotonic() - _t0,
        )
    probe_sec = _t.monotonic() - _t0
    # Drive's videoMediaMetadata is display-oriented — trust it over ffprobe.
    if drive_width and drive_height:
        w, h = int(drive_width), int(drive_height)

    probe = MediaProbe(
        width=w, height=h, duration_sec=dur,
        mime_type=drive_mime or "video/quicktime",
        extension=ext,
        checksum_md5=drive_md5 or _md5(local_path),
        size_bytes=os.path.getsize(local_path),
    )

    # V3.0: >10min routes to NeedsReview with reason LongVideo (in validator).
    # We do not sample frames for long clips — the caller flags them without
    # incurring a Gemini call.
    if dur > v.LONG_VIDEO_THRESHOLD_SEC:
        return SampledMedia(probe=probe, frames_jpeg=[], frame_timestamps_sec=[],
                            long_video=True, probe_sec=probe_sec)

    count = v.SHORT_VIDEO_FRAME_COUNT if dur <= v.SHORT_VIDEO_MAX_SEC else v.LONG_VIDEO_FRAME_COUNT
    timestamps = _evenly_spaced_timestamps(dur, count)

    _t1 = _t.monotonic()
    frames: List[bytes] = []
    kept_ts: List[float] = []
    for ts in timestamps:
        try:
            jpeg = _extract_frame_jpeg(local_path, ts, max_edge=max_edge)
            if jpeg:
                frames.append(jpeg)
                kept_ts.append(ts)
        except subprocess.CalledProcessError:
            # Skip a bad frame rather than fail the whole clip.
            continue
    extract_sec = _t.monotonic() - _t1

    if not frames:
        return SampledMedia(probe=probe, frames_jpeg=[], frame_timestamps_sec=[],
                            unsupported_reason="CorruptFile: no frames extracted",
                            probe_sec=probe_sec, extract_sec=extract_sec)

    return SampledMedia(probe=probe, frames_jpeg=frames, frame_timestamps_sec=kept_ts,
                        probe_sec=probe_sec, extract_sec=extract_sec)


def sample_image(local_path: str, *, drive_md5: Optional[str] = None,
                 drive_mime: Optional[str] = None, max_edge: int = 1600) -> SampledMedia:
    """Load an image, downscale to max_edge, return a single JPEG frame.
    HEIC is transcoded to JPEG on the fly via ffmpeg."""
    ext = Path(local_path).suffix.lower()
    if ext not in v.SUPPORTED_IMAGE_EXTENSIONS:
        return SampledMedia(
            probe=MediaProbe(0, 0, 0.0, drive_mime or "", ext, drive_md5 or "", os.path.getsize(local_path)),
            frames_jpeg=[], frame_timestamps_sec=[],
            unsupported_reason="UnsupportedFormat",
        )
    # Reuse ffmpeg to load + downscale + reencode to JPEG uniformly.
    try:
        jpeg = subprocess.check_output([
            "ffmpeg", "-nostdin", "-loglevel", "error",
            "-i", local_path,
            "-vf", f"scale='if(gt(iw,ih),min({max_edge},iw),-2)':'if(gt(iw,ih),-2,min({max_edge},ih))'",
            "-q:v", "3",
            "-f", "image2pipe", "-vcodec", "mjpeg", "-",
        ])
    except subprocess.CalledProcessError as e:
        return SampledMedia(
            probe=MediaProbe(0, 0, 0.0, drive_mime or "", ext, drive_md5 or _md5(local_path), os.path.getsize(local_path)),
            frames_jpeg=[], frame_timestamps_sec=[],
            unsupported_reason=f"CorruptFile: {e}",
        )
    # Probe dimensions from the emitted JPEG so we don't need PIL.
    w, h = _probe_image_dims(jpeg)
    probe = MediaProbe(
        width=w, height=h, duration_sec=0.0,
        mime_type=drive_mime or "image/jpeg",
        extension=ext,
        checksum_md5=drive_md5 or _md5(local_path),
        size_bytes=os.path.getsize(local_path),
    )
    return SampledMedia(probe=probe, frames_jpeg=[jpeg], frame_timestamps_sec=[0.0])


def extract_audio_mp3(
    local_path: str,
    *,
    max_duration_sec: float = 300.0,
) -> tuple[Optional[bytes], Optional[str]]:
    """Extract audio from a video file as a compressed mono MP3.

    Returns:
        (mp3_bytes, None)                  — success; send to Gemini for speech analysis
        (None, "NoAudioStream")            — video has no audio track; voiceover = null is valid
        (None, "AudioExtractionError:…")   — ffmpeg failure; must block auto-rename per V3.5

    V3.5 rule: audio-analysis failure is NOT the same as blank Voiceover. Only
    "NoAudioStream" allows a null Voiceover without blocking. Any other failure
    string must trigger NeedsReview / AudioAnalysisFailure in the validator.
    """
    ext = Path(local_path).suffix.lower()
    if ext not in v.SUPPORTED_VIDEO_EXTENSIONS:
        return None, "NoAudioStream"  # images and unknown types — not a failure

    # First confirm the file has an audio stream. A deliberately muted video is
    # not an analysis failure.
    try:
        probe_out = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_type",
                "-of", "default=noprint_wrappers=1:nokey=1",
                local_path,
            ],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except subprocess.CalledProcessError as e:
        return None, f"AudioExtractionError:ffprobe failed: {e}"

    if not probe_out:
        return None, "NoAudioStream"

    # Extract: mono, 16 kHz, 64 kbps MP3 — ~60 KB/min; stays well under inline limits.
    # Clamp to max_duration_sec so a 3-hour clip doesn't bloat the payload.
    try:
        cmd = [
            "ffmpeg", "-nostdin", "-loglevel", "error",
            "-t", str(max_duration_sec),
            "-i", local_path,
            "-vn",                   # drop video stream
            "-acodec", "libmp3lame",
            "-ab", "64k",
            "-ar", "16000",
            "-ac", "1",
            "-f", "mp3",
            "pipe:1",
        ]
        mp3_bytes = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
        if not mp3_bytes:
            return None, "AudioExtractionError:empty output"
        return mp3_bytes, None
    except subprocess.CalledProcessError as e:
        return None, f"AudioExtractionError:ffmpeg failed: {e}"


def _probe_image_dims(jpeg_bytes: bytes) -> tuple[int, int]:
    """Read dimensions from a JPEG blob using ffprobe on stdin."""
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height",
         "-of", "default=noprint_wrappers=1:nokey=1", "-"],
        input=jpeg_bytes, capture_output=True, check=True,
    )
    out = p.stdout.decode().strip().splitlines()
    return int(out[0]), int(out[1])
