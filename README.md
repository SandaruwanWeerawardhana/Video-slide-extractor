<div align="center">

# Video Slide Extractor

A command-line tool that pulls one clean screenshot per presentation slide out of a lecture/screencast recording — automatically, without manual scrubbing.
`

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


## Running the Project

### Windows Command Prompt (cmd)

Open **Command Prompt** and run:

```cmd
cd /d D:\Project\Personal\video-slide-extractor
ss "C:\path\to\your-video.mp4"
```

Example:

```cmd
cd /d D:\Project\Personal\video-slide-extractor
ss "C:\Users\weera\OneDrive\Desktop\OS\w1.mp4"
```

Change save folder and image format:

```cmd
cd /d D:\Project\Personal\video-slide-extractor
ss "C:\Users\weera\OneDrive\Desktop\OS\2.mp4" "C:\Users\weera\OneDrive\Desktop\OS\slides-output" jpg
```

Browser/direct MP4 link example:

```cmd
cd /d D:\Project\Personal\video-slide-extractor
ss "https://example.com/video.mp4"
```

The MP4 link is downloaded into `input\downloads\`, then screenshots are extracted.

The extracted screenshots are saved in `slides\`.

Manual fallback:

```cmd
cd /d D:\Project\Personal\video-slide-extractor
.venv\Scripts\python.exe extract_slides.py "C:\path\to\your-video.mp4" --output slides
```

### PowerShell / macOS / Linux

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

## Stopping the Project

This is a command-line script, not a server. It stops automatically when the extraction finishes.

To stop it while it is running, press:

```cmd
Ctrl + C
```

To leave the Python virtual environment in **Command Prompt**, run:

```cmd
.venv\Scripts\deactivate.bat
```

If that command does not work, close the Command Prompt window. The project is not still running after the script exits.

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
