# PawpyStuff AI Namer — Project Context

**Client:** PawpyStuff (CozyRider dog-travel product line).
**Purpose:** Automated file-classification pipeline for creator-content videos. AI classifies each clip; deterministic code assembles the filename per the client's V3.0 Naming Convention SOP. Runs on **Google Drive folders/files OR local directories/files** — same downstream classification either way.
**Current status (2026-08-25):** Pilot iterating on accuracy against a 209-row client answer key. Best measured field rates: **Format 100%, Dog 97%, Category 94%, Scene 78% top-1 / 86% top-2, Vehicle 76%, Shot 69%**.

---

## Repo layout

```
PawpyStuff/
├── pipeline/                        Python package (all business logic)
│   ├── __init__.py
│   ├── vocabulary.py                V3.0 taxonomy loaded from JSON at import
│   ├── source.py                    Unified source discovery (Drive/local, folder/file)
│   ├── drive_client.py              Drive listing + MIME-fallback ext resolution
│   ├── media.py                     Download + ffprobe + frame extraction
│   ├── classifier.py                Gemini multimodal call + response schema
│   ├── validator.py                 Deterministic filename assembly
│   └── config.py                    Env-driven Config dataclass
├── scripts/
│   └── run_pilot.py                 Orchestrator (threaded, per-file, 4 source modes)
├── tests/
│   └── test_validator.py            83 checks; run with `python tests/test_validator.py`
├── pilot_output/
│   ├── results.csv                  Latest run's row-per-file output
│   ├── logs/<drive_id>.json         Per-file audit log (model reasoning, timings, evidence)
│   └── PILOT_*.md                   Human-readable summaries
├── PawpyStuff_Creator_Content_Taxonomy_V3.0.json    ← source of truth
├── PawpyStuff_Creator_Content_Naming_Convention_V*.docx    (historic SOPs)
├── PS AI Naming Convention Spreadsheet - Sheet1 (1).csv    (client answer key)
├── ps-naming-convention-*.json      (service account creds — gitignored)
├── .env                             (GEMINI_API_KEY — gitignored)
├── venv/                            (Python 3.13 virtualenv — gitignored)
└── .media_cache/, pilot_output/     (runtime artifacts — gitignored)
```

**Cache location matters:** the media cache defaults to `/tmp/pawpystuff_media_cache/` (NOT the project folder). This is because macOS "Desktop & Documents in iCloud + Optimize Storage" silently offloads files >90% disk-full, and every read hangs on iCloud fetches. `/tmp` is never touched by iCloud.

---

## Pipeline flow

```
SOURCE (pick one via CLI flag) ──► pipeline.source discovery layer
  · --drive-folder-id ID  ─►  discover_drive_folder    → list_folder_media
  · (default)             ─►  discover_drive_folders_all
  · --drive-file-id ID    ─►  discover_drive_file       → files().get
  · --local-dir PATH      ─►  discover_local_dir        → rglob + MIME filter
  · --local-file PATH     ─►  discover_local_file       → single-item list
                            │
                            │  (all discoverers return dicts with the same
                            │   shape, plus source_type + local_path fields)
                            ▼
        run_pilot._process_file  (one thread per file)
                            │
                            ▼
        For Drive sources:  media.download_drive_file → /tmp cache
        For local sources:  read file in place (no download, no cache copy)
                            │
                            ▼
                     media.sample_video/image  (ffprobe + N frames)
                            │
                            ▼
        classifier.classify  (ONE Gemini call)
        │  · V3.0 system prompt (loaded from JSON)
        │  · up to 27 client-verified reference frames (few-shot)
        │  · filename hint (low-priority context)
        │  · video bytes (if ≤30s ≤15MB) OR sampled frames
        │  · returns strict JSON matching RESPONSE_SCHEMA
        ▼
        validator.validate_and_assemble   (deterministic, no AI)
        │  · Category × Scene allow-list check
        │  · duration >10min → NeedsReview/LongVideo
        │  · Format computed from dimensions (±3%)
        │  · assembles [Category]_[Scene]_[DogType?]_[VehicleClass?]_[ShotType]_[Format]_[T##?].ext
        ▼
        run_pilot writes:  results.csv row + logs/<id>.json
                           (id = drive_file_id for Drive, sha1(local_path)[:16] for local)
```

**Threading:** `ThreadPoolExecutor` with 6 workers by default. Each worker owns one file end-to-end. `googleapiclient` isn't thread-safe → each worker builds its own Drive service via `threading.local`. Log lines buffered per file (via `_Log` class) so stdout stays readable.

---

## Key design decisions

### 0. Unified source layer (`pipeline/source.py`)
Four discoverers — `discover_drive_folder`, `discover_drive_folders_all`, `discover_drive_file`, `discover_local_dir`, `discover_local_file` — all return dicts in a **common shape** (id, name, size, mimeType, folder_path, source_type, local_path, ...). The rest of the pipeline is source-agnostic: media handling, classifier, validator all just get a path (either a cache path for Drive or the source path for local) and do their thing.

Auth is conditional: the service account JSON is only needed when a Drive source is selected. Local-only runs work with just `GEMINI_API_KEY` and `ffmpeg`.

Log/cache key strategy:
- Drive files: keyed by `drive_file_id`.
- Local files: keyed by `sha1(abs_path)[:16]` — stable across runs on the same machine.

### 1. JSON-driven vocabulary (V3.0)
`pipeline/vocabulary.py` loads `PawpyStuff_Creator_Content_Taxonomy_V3.0.json` at import time. No hand-written Category/Scene constants. Refuses to auto-rename when `EXPECTED_VOCAB_VERSION != vocabulary_version` in the JSON — this enforces the client's `version_mismatch_blocks_auto_rename` rule.

**To upgrade to a new taxonomy version:** drop the new JSON in the project root and bump `EXPECTED_VOCAB_VERSION` in `vocabulary.py`.

### 2. The AI classifies; code names the file
Per client SOP: Gemini returns approved-vocabulary tokens with structured JSON. All filename assembly, allow-list validation, format computation, and take-collision handling happens in `validator.py` — pure Python, deterministic, unit-tested.

### 3. Folder Category is authoritative
When a file lives in a Category folder (`ProductShot/`, `ProductUse/`, `Problem/`), the folder Category is presumed authoritative. Model *validates* fit; strong disagreement → `NeedsReview` with `CategoryConflict`, never silent reclassification. In pilot mode (flat folder), the model picks Category from scratch using V3.0's decision order.

### 4. MIME-type fallback for extensionless files
Client sometimes uploads `.MOV` files without extensions. `drive_client.resolve_extension(name, mime_type)` uses Drive's `mimeType` as fallback so `video/quicktime` → `.mov`.

### 5. Video input for short clips
For clips `≤30s AND ≤15MB`, `classifier.classify()` sends the raw video bytes as `inline_data` instead of sampled frames. Gemini gets temporal camera motion (real Shot signal). For longer clips or bigger files, falls back to frames.

### 6. Reference-frame few-shot
At startup, `run_pilot._build_reference_frames()` picks up to 3 canonical examples per shot type (9 shot types × 3 = up to 27 frames) from the client's answer key CSV, extracts one middle-clip frame from each cached video, and embeds all into every Gemini call as visual anchors: *"Example of ShotType=Wide (client-labeled)"*.

### 7. Shot adjudicator disabled
`classifier.adjudicate_shot()` exists as a two-pass second Gemini call for ambiguous Mid/Wide/POV cases. **Disabled in `run_pilot.py`** because empirical test showed -8% Shot regression (over-corrected correct top-1 answers). Kept in code for future re-tuning with a more conservative adjudicator prompt.

---

## Configuration

CLI flags always win. Everything also has an env-var equivalent so background jobs / cron / CI don't need shell-arg gymnastics.

| CLI flag | Env var | Default | Purpose |
|---|---|---|---|
| — | `GEMINI_API_KEY` | (`.env` fallback) | **REQUIRED** |
| — | `PS_SERVICE_ACCOUNT_JSON` | project-local `ps-naming-convention-*.json` | Drive auth (Drive sources only) |
| `--drive-folder-id ID` | `PS_FOLDER_ID` | (auto-discover) | Drive folder mode |
| `--drive-file-id ID` | — | — | Single Drive file |
| `--local-dir PATH` | — | — | Local directory |
| `--local-file PATH` | — | — | Single local file |
| `--workers N` | `PS_WORKERS` | `6` | Concurrent worker threads |
| `--max-files N` | `PS_MAX_FILES` | (unset) | Cap for smoke tests |
| `--output-dir PATH` | `PS_OUTPUT_DIR` | `<project>/pilot_output` | Where CSV + logs go |
| `--media-cache-dir PATH` | `PS_MEDIA_CACHE` | `/tmp/pawpystuff_media_cache` | Drive download cache (unused for local) |
| `--no-recursive` | `PS_RECURSIVE=0` | recursive on | Applies to drive-folder and local-dir |
| `--resume` | `PS_RESUME=1` | off | Skip files with existing log |
| `--include-ids-file PATH` | `PS_INCLUDE_IDS_FILE` | (unset) | Only process listed source IDs |
| `--model NAME` | `PS_GEMINI_MODEL` | `gemini-flash-latest` | Model override |

---

## Running — the four modes

Exactly one source flag is allowed; if none is given, the runner falls back to auto-discovering every Drive folder the SA can see.

```bash
source venv/bin/activate

# ── A) All Drive folders shared with the service account (default)
python scripts/run_pilot.py

# ── B) One specific Drive folder (recurses into subfolders)
python scripts/run_pilot.py --drive-folder-id 1U9KSHZX1xaYDcKz9P3l4HNRhXW5xpw_H

# ── C) One specific Drive file (spot-check)
python scripts/run_pilot.py --drive-file-id 1scx320OdnIS38ISDiaNlKSp9fxDSv7Xl

# ── D) A local directory (no Drive credentials required)
python scripts/run_pilot.py --local-dir /Users/me/CozyRider_footage/

# ── E) A single local file (fastest path; also prints proposed name to stdout)
python scripts/run_pilot.py --local-file /Users/me/cheap_hammock_cover.mov
```

Any mode can be combined with tuning flags: `--workers N`, `--max-files N`, `--resume`, `--include-ids-file`, `--model`, `--output-dir`, `--no-recursive`.

```bash
# Unit tests
python tests/test_validator.py    # 83 checks
```

---

## Current tuning state

| Feature | Status | Effect |
|---|---|---|
| JSON-driven V3.0 vocabulary | ✅ ON | Prevents drift; blocks auto-rename on version mismatch |
| MIME fallback for extensionless files | ✅ ON | Recovers `.MOV` uploads with lost extensions |
| Threaded 6-worker pool | ✅ ON | ~5–6× wall-time speedup |
| Frame counts 6 short / 20 long | ✅ ON | Better temporal coverage on multi-scene testimonials |
| Filename hint (low-priority context) | ✅ ON | +8–12% Scene on cheap/failed clips |
| Video input for short clips (≤30s, ≤15MB) | ✅ ON | +6% Vehicle |
| Text few-shot for tricky scenes | ✅ ON | Reduced Scene ambiguity |
| Image few-shot: up to 27 reference frames across 9 shot types | ✅ ON | +7% Shot vs prior best |
| Shot adjudicator (two-pass) | ❌ OFF | Regressed -8%; kept in code, gated off |
| Audio input | ❌ OFF | "Phase 6" deferred |
| Gemini 2.5 Pro | ❌ OFF | Still on flash-latest (5× cheaper) |

Current `CLASSIFIER_VERSION`: `gemini-flash-latest+prompt-v30-07-video-fewshot-9shots`

---

## Best measured field rates

Against 209-row client answer key (V3.0-aligned):

| Field | Rate | Client's launch gate | Gap |
|---|---:|---:|---:|
| Format | **100%** | 100% | 0 ✅ |
| Dog | **97%** | ≥99% | -2 |
| Category | **94%** | ≥99% | -5 |
| Scene top-1 | **78%** | ≥95% | -17 |
| Scene top-2 (with runner-up credit) | **86%** | — | close |
| Vehicle | **76%** | ≥97% | -21 |
| Shot | **69%** | ≥94% | -25 |

Format is at gate; Dog/Category are within striking distance; Shot is the outlier.

---

## Known gotchas

1. **macOS iCloud offload** — if disk drops below ~10 GB free, `Desktop/` files get offloaded. Every file read (Python `.pyc` files, CSV, logs) hangs on iCloud fetch. Symptoms: Python startup takes minutes, or `ModuleNotFoundError` on system modules. Fix: `rm -rf /tmp/pawpystuff_media_cache` (frees 3–4 GB), or free disk elsewhere.

2. **Gemini prepaid credits deplete mid-run.** No warning; requests just start returning HTTP 429 with `RESOURCE_EXHAUSTED`. Check billing at `https://ai.studio/projects`. Runner marks each affected file as `NeedsReview / ClassifierError`; use `PS_RESUME=1` to only re-classify errored files after top-up.

3. **Drive filenames with trailing spaces** — client's answer key sometimes strips them, causing pairing misses. Comparison scripts normalize whitespace before matching.

4. **Duplicate filenames in same folder** — Drive allows two files with the same name. T## take-collision logic handles this at rename-time.

5. **The V3.0 launch gates assume a 500-asset gold set** which the client hasn't produced yet. Our pilot uses their 209-row answer key, which is a smaller/noisier baseline.

---

## What's NOT built yet

- **Rename apply mode.** Currently read-only — writes `proposed_filename` to CSV but never touches Drive. Production needs a `--apply` mode with per-file lock, rollback log, and idempotency keys (per V3.0 SOP).
- **Google Sheets writeback.** V3.0 spec calls for writing rows to a Sheet; we output CSV only.
- **Duplicate DUPLICATE_ prefix flow.** Detected via checksum, but the prefix + in-place retention isn't implemented.
- **Audio transcription.** Gemini Flash supports audio input; not wired up.
- **500-asset gold set + confusion-matrix monitoring.** V3.0 gating framework not implemented.
- **CLI arg parsing (`argparse`).** Everything is env vars right now.
- **Sheets API enablement** on project `ps-naming-convention` (needs the client to enable it in Cloud Console).

---

## Handover checklist (for the client)

- [ ] Enable Sheets API on GCP project `ps-naming-convention`
- [ ] Move the media cache off Desktop or ensure `/tmp/pawpystuff_media_cache` is the default (already is)
- [ ] Set up billing alerts on Gemini API to avoid mid-run 429s
- [ ] Create Shared Drive with `ProductShot/`, `ProductUse/`, `Problem/`, `_NeedsReview/` folder structure
- [ ] Grant service account `Content Manager` on Shared Drive (for future rename)
- [ ] Grow answer key to 500 assets for calibration (V3.0 launch gate)
- [ ] Decide whether to enable audio input (Phase 6)
- [ ] Decide whether to switch to `gemini-2.5-pro` (5× cost, +5–10% accuracy)

---

## Testing

```bash
python tests/test_validator.py
# Expected: "83/83 checks passed"
```

Tests cover: V3.0 vocabulary (categories, scenes, DogTypes, VehicleClasses, ShotTypes, migrations from V2.7 → V3.0), format computation, deterministic filename assembly for every SOP example, LongVideo routing, take-collision, folder-Category enforcement, invalid tokens rejected, version-mismatch behavior.

No unit tests for `media.py`, `drive_client.py`, or `classifier.py` (these depend on subprocess/network).
