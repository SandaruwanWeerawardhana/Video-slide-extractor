<div align="center">

# Video Slide Extractor

A command-line tool that pulls one clean screenshot per presentation slide out of a lecture/screencast recording — automatically, without manual scrubbing.

</div>

## Overview

Recorded lectures and webinars often mix a slide deck with a talking head, a moving cursor, and video-compression noise. Manually scrubbing through the recording to grab a screenshot of each slide is slow and error-prone.

`extract_slides.py` samples a video a few times per second, compares consecutive frames using structural similarity (SSIM), and writes a screenshot to disk only once a *new* picture has been visibly stable for a moment — skipping transition frames, fades, and duplicate slides, and merging multi-step "build" animations (e.g. PowerPoint bullet-by-bullet reveals) into a single final screenshot.

**Who it's for:** students, teachers, and anyone archiving lecture recordings, webinars, or screen-shared meetings who wants a clean set of slide images without watching the whole video.

**Main objectives:**
- Detect real slide changes while ignoring codec noise, webcam overlays, and moving cursors/progress bars.
- Avoid duplicate screenshots from fades and re-appearing slides.
- Produce timestamped, ordered output with machine-readable metadata (JSON) and a human-readable summary (TXT).

## Features

### Slide Detection
- SSIM-based structural comparison (immune to compression artifacts, brightness drift)
- Information-weighted similarity — text/shape regions matter more than empty background
- Two-state machine (`stable` / `transition`) that waits for the picture to settle before capturing
- Automatic noise-floor estimation per video (adapts the "stable" threshold to each recording's own grain)
- Automatic motion masking — learns and down-weights permanently-moving regions (webcam box, clock, progress bar)
- Build/animation merging — a slide that reappears too soon after the last capture replaces it instead of adding a duplicate
- Duplicate guard for fade-out/fade-in back to the same slide

### Region of Interest
- `--crop X,Y,WIDTH,HEIGHT` to restrict detection to part of the frame (e.g. ignore a webcam corner)
- Optional `--crop-output` to also crop the saved screenshots

### Video Handling
- Sequential, low-memory reading (frame-skipping via `cap.grab()`, not full decode) — a multi-hour 1080p video costs megabytes, not gigabytes
- `--start` / `--end` trimming with `SS`, `MM:SS`, or `HH:MM:SS` time formats
- Robust handling of unreliable container metadata (FPS/frame-count fallbacks)

### Output
- PNG (default) or JPEG output, with configurable JPEG quality
- PNG metadata embedding (source file, slide number, timestamp) via Pillow, when installed
- `slides.json` (machine-readable) and `slides.txt` (human-readable) summary written per run
- Progress bar with percent, elapsed time, processing speed, and ETA

### Debugging
- `--debug` mode: verbose logging plus before/after/diff images and a per-sample `similarity.csv` for tuning thresholds

### Configuration
- All parameters tunable via CLI flags or a JSON config file (`--config`)
- `--write-config` to generate an example config file
- Config validation with readable error messages; common misconfigurations (e.g. threshold ordering) are auto-corrected

## Tech Stack

| Category | Technology |
|----------|------------|
| Language | Python 3 (uses `from __future__ import annotations`, PEP 604 syntax) |
| Video decoding | OpenCV (`opencv-python`) |
| Numerical processing | NumPy |
| Image metadata | Pillow (optional, PNG `tEXt` chunks) |
| Interface | `argparse` CLI (no web/GUI layer) |
| Testing | Not implemented |
| Deployment | Not implemented (local script) |

This is a standalone script-based tool — there is no frontend, backend server, database, or API in this project.

## Architecture

Layered, single-responsibility modules with no framework — plain Python classes and dataclasses:

- **`extract_slides.py`** — CLI entry point. Parses arguments, builds `Config`, drives the extraction loop, writes output files (`SlideWriter`, `DebugRecorder`).
- **`video.py`** — `VideoSource`: wraps `cv2.VideoCapture`, exposes a sequential frame-sampling iterator and video metadata (`VideoInfo`).
- **`detector.py`** — `SlideDetector`: the core state machine. Computes SSIM, tracks `MotionMask` and `NoiseFloor`, decides when a slide has changed and settled, emits `SlideEvent`s.
- **`config.py`** — `Config` dataclass holding every tunable parameter, with JSON load/validate support.
- **`utils.py`** — shared helpers: time formatting, safe image writing, directory checks, logging setup, terminal progress display.

**Data flow:**

```
video file
   │
   ▼
VideoSource.iter_samples()  ──►  sampled (timestamp, frame_index, frame)
   │
   ▼
SlideDetector.process()  ──►  SSIM vs reference/candidate, motion mask, noise floor
   │
   ▼  (0 or 1 SlideEvent per sample)
SlideWriter.add()  ──►  screenshot written to disk
   │
   ▼
SlideWriter.write_metadata()  ──►  slides.json + slides.txt
```

## Folder Structure

```
video-slide-extractor/
├── extract_slides.py   # CLI entry point, output writers, main extraction loop
├── detector.py          # SSIM math, motion masking, noise floor, slide state machine
├── video.py              # OpenCV video reader / frame sampler
├── config.py             # Config dataclass, validation, JSON load/save
├── utils.py               # time/image/logging/progress helpers
├── requirements.txt       # opencv-python, numpy, Pillow
└── output/                # default destination for extracted slides (created at runtime)
```

There are no `src/`, `controllers/`, `models/`, or `routes/` directories — the project is four flat Python modules plus the CLI script.

## Database

Not implemented. This tool does not use a database; results are written directly to the filesystem (`output/` directory) as image files plus `slides.json` / `slides.txt`.

## API Documentation

Not implemented. This is a CLI-only tool with no HTTP server or API endpoints.

**CLI interface** (in place of an API table):

| Flag | Description | Required |
|------|-------------|----------|
| `video` (positional) | Path to the video file | Yes (unless `--write-config`) |
| `-o, --output` | Output directory for screenshots (default: `output`) | No |
| `-r, --sample-rate` | Frames analysed per second (default: `2.0`) | No |
| `-t, --threshold` | Change-detection SSIM threshold (default: `0.90`) | No |
| `--min-slide-duration` | Minimum seconds before a new picture counts as its own slide (default: `2.0`) | No |
| `-f, --format` | Output format: `png` or `jpg` (default: `png`) | No |
| `--stability-threshold` | SSIM above which consecutive frames are "not moving" (default: `0.985`) | No |
| `--stability-duration` | Seconds a picture must hold still before capture (default: `1.0`) | No |
| `--duplicate-threshold` | SSIM above which a candidate is dropped as duplicate (default: `0.98`) | No |
| `--crop` | Detection region `X,Y,WIDTH,HEIGHT` | No |
| `--crop-output` | Also crop saved screenshots | No |
| `--no-auto-mask` | Disable automatic motion masking | No |
| `--work-width` | Downscale width used for comparison (default: `320`) | No |
| `--start` | Skip video before this time | No |
| `--end` | Stop at this time | No |
| `--jpeg-quality` | JPEG quality 1–100 (default: `95`) | No |
| `--config` | JSON file with settings | No |
| `--write-config` | Write an example config file and exit | No |
| `--debug` | Verbose logging + before/after/diff debug images | No |
| `--debug-dir` | Debug output directory (default: `debug`) | No |
| `-q, --quiet` | Warnings/errors only | No |
| `--version` | Print version and exit | No |

## Installation

1. Clone the repository
   ```bash
   git clone <repository-url>
   cd video-slide-extractor
   ```
2. Create and activate a virtual environment
   ```bash
   python -m venv .venv
   .venv\Scripts\activate      # Windows
   source .venv/bin/activate   # macOS/Linux
   ```
3. Install dependencies
   ```bash
   pip install -r requirements.txt
   ```
4. No environment variables, database migrations, or seed data are required — this is a stateless CLI tool.

## Environment Variables

Not implemented. This project has no `.env` file and reads no environment variables; all configuration is via CLI flags or a JSON config file (see `EXAMPLE_CONFIG` in [config.py](config.py)).

## Running the Project

```bash
# Basic usage
python extract_slides.py lecture.mp4

# Custom output directory, sample rate, and threshold
python extract_slides.py lecture.mp4 --output slides --sample-rate 2 --threshold 0.90

# Ignore a webcam strip in the bottom-right (detection only)
python extract_slides.py lecture.mp4 --crop 0,0,1920,900

# Debug mode: explains every decision + writes before/after/diff images
python extract_slides.py lecture.mp4 --debug

# Generate an example config file, then run with it
python extract_slides.py --write-config myconfig.json
python extract_slides.py lecture.mp4 --config myconfig.json

# Show CLI help / version
python extract_slides.py --help
python extract_slides.py --version
```

**Build / Test / Lint / Formatting:** Not implemented — no `setup.py`/`pyproject.toml`, test suite, linter, or formatter config exists in this project.

## Screenshots

_Not available in this repository._ Output is a set of `slide_NNN.png` (or `.jpg`) images written to the configured output directory, plus `slides.json` and `slides.txt` summarizing each run.

## Authentication Flow

Not implemented. This is a local CLI tool with no login, sessions, users, or roles.

## Error Handling

- Custom exception hierarchy rooted at `SlideExtractorError` ([utils.py](utils.py)), with `VideoError`, `OutputError`, and `ConfigError` ([config.py](config.py)) subtypes for specific failure domains.
- `Config.validate()` rejects invalid parameter combinations (e.g. thresholds out of `(0, 1]` range, `stability_threshold < change_threshold`, bad crop dimensions) with human-readable messages before any processing starts.
- Video errors are caught at the source: missing/empty files, unsupported codecs, and undecodable frames all raise `VideoError` with actionable suggestions (e.g. an `ffmpeg` re-encode command).
- The CLI (`main()` in [extract_slides.py](extract_slides.py)) catches `SlideExtractorError`, `ValueError`, and `MemoryError`, printing a clear message and returning a distinct **process exit code** per failure class instead of an HTTP status (there is no server):
  - `0` — success
  - `1` — video/output error, or out-of-memory
  - `2` — configuration/argument error
  - `3` — no slides detected
  - `130` — interrupted (Ctrl+C)
- `KeyboardInterrupt` during extraction is caught mid-run so partial results (slides found so far) are still written to `slides.json`/`slides.txt` before exiting.

## Performance Optimizations

- **Cheap frame skipping**: only sampled frames are fully decoded (`cap.read()`); frames in between are skipped with `cap.grab()`, which avoids full decode cost ([video.py](video.py)).
- **Downscaled comparison**: all SSIM comparisons run on a small (default 320px-wide) grayscale image (`work_width` in [config.py](config.py)), not the full-resolution frame; screenshots are still saved at original resolution.
- **Sequential, low-memory streaming**: only one frame is held in memory at a time — a multi-hour 1080p video costs megabytes, not gigabytes.
- **Auto motion masking**: permanently busy regions (webcam, clock) are down-weighted so the detector doesn't waste cycles/re-triggers on irrelevant motion.
- **Adaptive noise floor**: per-video stability threshold estimation avoids both false triggers (too strict) and missed slides (too loose) without manual per-video tuning.
- No caching, CDN, or image-optimization pipeline is applicable — this is an offline, single-machine batch tool, not a served application.

## Security

Not applicable in the traditional web-app sense — this tool has no network listener, no user accounts, and no database.

Relevant local-file safety measures that do exist:
- Path validation before opening a video (existence, non-directory, non-empty) — [video.py](video.py) `_validate_path`.
- Output/debug directories are verified writable before use, with a probe-file write test — [utils.py](utils.py) `ensure_directory`.
- Config files are parsed strictly: unknown JSON keys raise `ConfigError` rather than being silently ignored — [config.py](config.py) `Config.from_file`.
- Not applicable: authentication, authorization, password hashing, CSRF, CORS, rate limiting, SQL injection, and XSS protections — none of these concerns exist for a local, single-user CLI script.

## Deployment

Not implemented. There is no Dockerfile, CI/CD workflow, or hosting configuration in this repository. The tool is intended to run locally via Python:

```bash
python extract_slides.py <video>
```

## Future Improvements

Based on the current codebase:
- Add an automated test suite (unit tests for `detector.py`'s SSIM/state-machine logic, `config.py` validation, and CLI argument parsing).
- Add `opencv-python-headless` as an alternative dependency option for headless/server environments (mentioned in [requirements.txt](requirements.txt) comments but not wired into `requirements.txt` itself).
- Package the project (`pyproject.toml`/`setup.py`) for `pip install` / console-script distribution instead of direct script invocation.
- Optional parallelization of frame sampling/SSIM computation for faster processing of very long videos.
- Batch mode to process multiple video files in one invocation.

## Contributing

No `CONTRIBUTING.md` exists yet. Suggested workflow for contributors:
1. Fork the repository and create a feature branch.
2. Keep changes focused — this project favors small, single-purpose modules.
3. Test manually against a sample video (and with `--debug` to inspect detection behavior) since no automated test suite currently exists.
4. Open a pull request describing the change and rationale.

## License

No `LICENSE` file is currently present in this repository. Add one (e.g. MIT, Apache-2.0) to clarify usage terms before public release.

## Author

- **Name:** _Add your name_
- **GitHub:** _Add your GitHub profile URL_
- **LinkedIn:** _Add your LinkedIn profile URL_
- **Email:** _Add your contact email_
