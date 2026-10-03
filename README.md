# PawpyStuff AI Namer

Automated file-classification pipeline for PawpyStuff creator-content videos. Follows the PawpyStuff Naming Convention SOP (V3.2). The AI classifies each clip; deterministic code assembles the filename.

**Read-only** — the runner writes proposed filenames to a CSV and per-file audit logs, but never renames or moves files anywhere.

**Runs on four kinds of input:**
- an entire **Google Drive folder** (recursive; the historic default)
- a **single Google Drive file**
- an entire **local directory** (recursive)
- a **single local file**

---

## Table of contents

1. [Setup from scratch — macOS](#setup-from-scratch--macos)
2. [Setup from scratch — Windows](#setup-from-scratch--windows)
3. [Configure credentials](#configure-credentials)
4. [Sanity-check the install](#sanity-check-the-install)
5. [Running — the four modes](#running--the-four-modes)
6. [Common combinations](#common-combinations)
7. [Viewing the results](#viewing-the-results)
8. [Configuration reference](#configuration-reference)
9. [Repo layout](#repo-layout)
10. [Supported media](#supported-media)
11. [Troubleshooting](#troubleshooting)

---

## Setup from scratch — macOS

You need: Homebrew, Git, Python 3.11+, ffmpeg, and (optionally) Claude Code. Steps below assume a fresh Mac.

### 1. Install Homebrew

Open **Terminal** (Cmd+Space, type "Terminal") and run:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Follow the on-screen prompts. When it finishes, follow the "Next steps" it prints — usually one line to add Homebrew to your `$PATH`.

Verify:

```bash
brew --version    # should print "Homebrew X.Y.Z"
```

### 2. Install Git, Python, ffmpeg, and Node.js

```bash
brew install git python@3.12 ffmpeg node
```

Verify each:

```bash
git --version         # git version 2.x
python3 --version     # Python 3.12.x
ffmpeg -version       # ffmpeg version ...
ffprobe -version      # ffprobe version ...
node --version        # v22.x or newer
npm --version         # 10.x or newer
```

### 3. Install Claude Code (optional but recommended)

Claude Code is the AI CLI used to iterate on this pipeline. Install with npm:

```bash
npm install -g @anthropic-ai/claude-code
```

Verify:

```bash
claude --version
```

The first time you run `claude` in a project directory, it will walk you through logging in with your Anthropic account.

### 4. Clone the repo

```bash
cd ~/Desktop
git clone <REPO_URL> PawpyStuff
cd PawpyStuff
```

Replace `<REPO_URL>` with the GitHub / Drive URL where the code lives. If you received the code as a folder (not git), just move that folder into `~/Desktop` and `cd` into it.

### 5. Create a Python virtual environment and install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

You should see `(venv)` at the start of your prompt after activating.

**Every future terminal session** in this project needs `source venv/bin/activate` first.

Skip to → [**Configure credentials**](#configure-credentials).

---

## Setup from scratch — Windows

You need: Git, Python 3.11+, ffmpeg, Node.js, and (optionally) Claude Code. Steps assume a fresh Windows 10 or 11 machine.

### 1. Install Git for Windows

Download from **https://git-scm.com/download/win** and run the installer. Accept defaults except:
- On "Adjusting your PATH environment" pick **"Git from the command line and also from 3rd-party software"**.

Verify by opening **PowerShell** (Start menu → type "PowerShell") and running:

```powershell
git --version
```

### 2. Install Python 3.12

Download from **https://www.python.org/downloads/windows/**. Run the installer and:
- ✅ **Check "Add python.exe to PATH"** at the bottom of the first screen.
- Click "Install Now".

Verify:

```powershell
python --version    # Python 3.12.x
pip --version
```

### 3. Install ffmpeg

Windows doesn't ship with ffmpeg. Easiest path — use **winget** (built into Windows 10/11):

```powershell
winget install --id=Gyan.FFmpeg -e
```

Close and re-open PowerShell so the new `PATH` takes effect. Verify:

```powershell
ffmpeg -version
ffprobe -version
```

If `winget` isn't available, download a static build from **https://www.gyan.dev/ffmpeg/builds/** (get "release full"), unzip to `C:\ffmpeg`, then add `C:\ffmpeg\bin` to your `PATH`:
- Start menu → "Edit environment variables for your account" → Path → Edit → New → paste `C:\ffmpeg\bin`.
- Restart PowerShell and re-run `ffmpeg -version`.

### 4. Install Node.js

Download the **LTS installer** from **https://nodejs.org/** and run it (defaults are fine). Verify:

```powershell
node --version
npm --version
```

### 5. Install Claude Code (optional but recommended)

```powershell
npm install -g @anthropic-ai/claude-code
claude --version
```

Log in with your Anthropic account the first time you launch `claude` in a project.

### 6. Clone the repo

```powershell
cd $HOME\Desktop
git clone <REPO_URL> PawpyStuff
cd PawpyStuff
```

### 7. Create a Python virtual environment and install dependencies

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If PowerShell blocks the activation script with an execution-policy error, run once:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

Then re-run `.\venv\Scripts\Activate.ps1`. You should see `(venv)` at the start of your prompt.

**Every future PowerShell session** in this project needs `.\venv\Scripts\Activate.ps1` first.

---

## Configure credentials

You need a **Gemini API key**. If you also want to classify Google Drive files, you additionally need a **Google service account** with Drive read access.

### 1. Gemini API key

1. Go to **https://aistudio.google.com/apikey** and create a key.
2. Add prepaid credit to the associated Google Cloud billing account (Gemini is pay-as-you-go).
3. Save the key to a `.env` file in the project root:

**macOS / Linux:**
```bash
echo 'GEMINI_API_KEY=your-key-here' > .env
```

**Windows PowerShell:**
```powershell
'GEMINI_API_KEY=your-key-here' | Out-File -Encoding utf8 .env
```

### 2. Google service account (only if you'll use Drive sources)

1. In Google Cloud Console → **IAM & Admin → Service Accounts** → create a service account.
2. **Keys → Add key → JSON** → downloads a JSON file. Save it inside the project folder as `ps-service-account.json` (or any name — see below).
3. Enable **Google Drive API** in the same GCP project.
4. Copy the service account's email (looks like `xxx@yyy.iam.gserviceaccount.com`).
5. In Google Drive, right-click the target folder(s) → **Share** → paste the service account email → set to **Viewer**.

Tell the pipeline where the JSON is (only needed for Drive modes):

**macOS / Linux:**
```bash
export PS_SERVICE_ACCOUNT_JSON="$PWD/ps-service-account.json"
```

**Windows PowerShell:**
```powershell
$env:PS_SERVICE_ACCOUNT_JSON = "$PWD\ps-service-account.json"
```

If you skip Drive setup, `--local-dir` and `--local-file` modes still work with just the Gemini key.

---

## Sanity-check the install

**macOS / Linux:**
```bash
source venv/bin/activate
python tests/test_validator.py        # expect "N/N checks passed"
python scripts/run_pilot.py --help    # prints all CLI flags
```

**Windows PowerShell:**
```powershell
.\venv\Scripts\Activate.ps1
python tests\test_validator.py
python scripts\run_pilot.py --help
```

If both commands succeed, you're set.

---

## Running — the four modes

The runner accepts **exactly one source flag**. Pick one:

> Windows note: swap `/` for `\` in local paths, and `python scripts/run_pilot.py` for `python scripts\run_pilot.py`. Every other flag is identical.

### A) Google Drive folder (auto-discover all shared)

Classify every media file the service account can see across every shared folder.

```bash
python scripts/run_pilot.py
```

### B) Google Drive folder — specific ID

Restrict to one folder. Recurses into subfolders by default (`--no-recursive` to disable).

```bash
python scripts/run_pilot.py --drive-folder-id 1U9KSHZX1xaYDcKz9P3l4HNRhXW5xpw_H
```

### C) Google Drive — single file

Classify one Drive file by ID. Perfect for spot-checking a specific clip. Prints the proposed filename to stdout at the end.

```bash
python scripts/run_pilot.py --drive-file-id 1scx320OdnIS38ISDiaNlKSp9fxDSv7Xl
```

### D) Local directory

Walk a local directory and classify every supported media file inside (recursive by default). **No Drive credentials needed.**

```bash
# macOS / Linux
python scripts/run_pilot.py --local-dir /Users/me/CozyRider_footage/

# Windows
python scripts\run_pilot.py --local-dir C:\Users\me\CozyRider_footage
```

### E) Local single file

Classify one file on disk. Fastest end-to-end path (no Drive listing, no download).

```bash
# macOS / Linux
python scripts/run_pilot.py --local-file /Users/me/cheap_hammock_cover.mov

# Windows
python scripts\run_pilot.py --local-file C:\Users\me\cheap_hammock_cover.mov
```

Output looks like:

```
Source:            cheap_hammock_cover.mov
Status:            AutoRenamed
Proposed filename: Problem_FailedSeatCover_OtherDog_SUV_Wide_9x16.mov
```

---

## Common combinations

```bash
# Smoke-test: 5 files
python scripts/run_pilot.py --local-dir ~/footage/ --max-files 5

# Resume: skip files that already have a per-file JSON log
python scripts/run_pilot.py --drive-folder-id 1AbC... --resume

# Targeted rerun on specific source IDs
python scripts/run_pilot.py --local-dir ~/footage/ \
    --include-ids-file /tmp/target_ids.txt

# Try the stronger (and pricier) Gemini 2.5 Pro
python scripts/run_pilot.py --model gemini-2.5-pro

# Non-recursive local walk (skip subfolders)
python scripts/run_pilot.py --local-dir ~/footage/ --no-recursive

# Sequential (single-worker) run — useful for debugging
python scripts/run_pilot.py --workers 1

# Custom output dir + cache location
python scripts/run_pilot.py \
    --drive-folder-id 1AbC... \
    --output-dir ~/PS_runs/2026-08-25 \
    --media-cache-dir /Volumes/BigDisk/pawpy_cache
```

---

## Viewing the results

Every run writes to `pilot_output/` (or the folder given with `--output-dir`):

| Artifact | What it is |
|---|---|
| `results.csv` | One row per file: proposed filename, classified tokens, per-stage timings, Gemini token counts, `source_type` |
| `logs/<id>.json` | Full audit log per file: model reasoning, evidence timestamps, classifier version, ffprobe metadata |
| `logs/<id>.error.json` | Only present if the classifier errored (rate limit, parse fail) |
| `PILOT_RESULTS.md` | Auto-generated human-readable summary |

Log ID is `drive_file_id` for Drive sources, `sha1(local_path)[:16]` for local sources.

### Opening the CSV

**macOS:**
```bash
open pilot_output/results.csv          # opens in Numbers or Excel
```

**Windows:**
```powershell
start pilot_output\results.csv          # opens in Excel
```

Or import into Google Sheets: File → Import → Upload → `results.csv`.

### Reading the markdown summary

**macOS:**
```bash
open pilot_output/PILOT_RESULTS.md      # opens in default markdown viewer
cat pilot_output/PILOT_RESULTS.md       # dump to terminal
```

**Windows:**
```powershell
start pilot_output\PILOT_RESULTS.md
type pilot_output\PILOT_RESULTS.md
```

### Inspecting a specific file's decision

Every classified file has a JSON log with the model's reasoning, evidence timestamps, and each candidate token it considered.

```bash
# macOS / Linux
cat pilot_output/logs/0002dd494574f9d0.json | python -m json.tool | less

# Windows
type pilot_output\logs\0002dd494574f9d0.json | python -m json.tool | more
```

Look at fields:
- `classification` — the AI's raw output
- `validation.assembled_name` — the deterministic filename we built from it
- `validation.review_reasons` — why the file needs review, if flagged
- `evidence_timestamps` — timestamps in the video the model cited as evidence
- `t_classify_sec` / `t_extract_sec` — per-stage timing

### Comparing against a client answer key

If the client provides a spreadsheet of expected filenames (rows: `Filename, Category, Scene, Dog, Vehicle, Shot, Format`), you can measure accuracy by pairing on filename. Ask the ops team for the current comparison script (`cmp_v32b.py`) or replicate the logic:

1. Load answer key rows.
2. Load `results.csv`.
3. Strip file extensions from both sides, normalize whitespace, lowercase.
4. Pair by normalized name and count matches per field.

---

## Configuration reference

All CLI flags map to environment variables. CLI flags override env vars.

| CLI flag | Env var | Default | Purpose |
|---|---|---|---|
| — | `GEMINI_API_KEY` | (required, from `.env`) | Gemini API key |
| — | `PS_SERVICE_ACCOUNT_JSON` | project-local `ps-naming-convention-*.json` | Drive auth (Drive sources only) |
| `--drive-folder-id ID` | `PS_FOLDER_ID` | (auto-discover) | Drive folder mode |
| `--drive-file-id ID` | — | — | Single Drive file |
| `--local-dir PATH` | — | — | Local directory |
| `--local-file PATH` | — | — | Single local file |
| `--workers N` | `PS_WORKERS` | `6` | Concurrent worker threads |
| `--max-files N` | `PS_MAX_FILES` | (unset) | Cap for smoke tests |
| `--output-dir PATH` | `PS_OUTPUT_DIR` | `<project>/pilot_output` | Where CSV + logs go |
| `--media-cache-dir PATH` | `PS_MEDIA_CACHE` | `/tmp/pawpystuff_media_cache` | Drive download cache |
| `--no-recursive` | `PS_RECURSIVE=0` | recursive on | Applies to drive-folder and local-dir |
| `--resume` | `PS_RESUME=1` | off | Skip files with existing log |
| `--include-ids-file PATH` | `PS_INCLUDE_IDS_FILE` | (unset) | Only process listed source IDs |
| `--model NAME` | `PS_GEMINI_MODEL` | `gemini-flash-latest` | Model override (e.g. `gemini-2.5-pro`) |
| `--version` | — | — | Print vocab + automation version |

---

## Repo layout

```
PawpyStuff/
├── pipeline/                          Python package (all business logic)
│   ├── vocabulary.py                  Loads V3.2 taxonomy from JSON at import
│   ├── source.py                      Unified source discovery (Drive/local, folder/file)
│   ├── drive_client.py                Drive listing + MIME-fallback ext resolution
│   ├── media.py                       Download + ffprobe + frame extraction
│   ├── classifier.py                  Gemini multimodal call + response schema
│   ├── validator.py                   Deterministic filename assembly
│   └── config.py                      Env-driven Config
├── scripts/run_pilot.py               Orchestrator (threaded, per-file, 4 source modes)
├── tests/test_validator.py            Vocabulary + assembly unit checks
├── PawpyStuff_Creator_Content_Taxonomy_V3.2.json    ← runtime contract
├── requirements.txt
├── CLAUDE.md                          Rich project context (for Claude Code)
└── README.md                          This file
```

See **`CLAUDE.md`** for design decisions, tuning history, accuracy rates, and gotchas.

---

## Supported media

- **Videos:** `.mov`, `.mp4` (Drive `video/quicktime`, `video/mp4`)
- **Images:** `.jpg`, `.jpeg`, `.png`, `.heic`
- **MIME fallback:** files without an extension are resolved from Drive's `mimeType` (Drive) or `mimetypes.guess_type()` (local).

Files > 10 minutes route to `NeedsReview` with reason `LongVideo` per V3.0 spec (no auto-classification).

---

## Troubleshooting

### "ModuleNotFoundError" or "No module named 'X'"

You didn't activate the virtualenv. Run `source venv/bin/activate` (macOS) or `.\venv\Scripts\Activate.ps1` (Windows) and try again.

### "ffmpeg: command not found" / "ffprobe: command not found"

ffmpeg isn't on your `PATH`. On macOS reinstall with `brew install ffmpeg`. On Windows, either use `winget install Gyan.FFmpeg` or verify `C:\ffmpeg\bin` is in your user PATH and restart the terminal.

### `HTTP 429 RESOURCE_EXHAUSTED` mid-run

Gemini prepaid credits ran out. Top up at **https://aistudio.google.com/apikey** → billing. Then re-run with `--resume` to skip already-classified files:

```bash
python scripts/run_pilot.py --local-dir ~/footage/ --resume
```

### macOS: Python startup takes minutes, or file reads hang

Your Mac has "Desktop & Documents in iCloud" + "Optimize Storage" enabled, and disk is >90% full. macOS silently offloads files to iCloud in this state, and every read blocks on a network fetch. Free 5+ GB of disk (e.g. `rm -rf /tmp/pawpystuff_media_cache`) and retry.

### Windows: `Activate.ps1 cannot be loaded because running scripts is disabled`

PowerShell's execution policy blocks unsigned scripts. Run once:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

Then re-activate the venv.

### "GEMINI_API_KEY not set"

Either `.env` is missing, in the wrong folder, or has the wrong variable name. Verify with:

```bash
# macOS / Linux
cat .env

# Windows
type .env
```

You should see exactly `GEMINI_API_KEY=...`. No quotes around the value.

### Drive files return "not found" or "permission denied"

The service account email isn't shared on the target folder. In Google Drive, right-click the folder → Share → paste the SA email → Viewer.

### The proposed filename is wrong for a specific clip

Open `pilot_output/logs/<id>.json` and read `classification.reasoning` — the model explains why it chose each token. Common cases:
- **Wrong Category:** the file lives outside a Category folder and the visual signal was ambiguous. Move it to `ProductShot/`, `ProductUse/`, or `Problem/` on Drive so folder Category is authoritative.
- **Wrong Scene:** the clip may match multiple scenes; check `scene_runner_up` for the model's second choice.
- **Wrong Shot:** shot-type accuracy plateaus around 78-80% on V3.2. Client-labeled reference frames drive the model; more examples in the answer key improve future runs.
