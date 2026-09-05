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

Argument order is `ss <video> [output-folder] [png|jpg] [extra flags...]`. Any
argument starting with `-` (and everything after it) is forwarded verbatim to
`extract_slides.py`, so all of its flags work through `ss`:

```cmd
cd /d D:\Project\Personal\video-slide-extractor

:: crop the detection region to the top-left 1920x900 (ignore a webcam strip)
ss w1.mp4 slides jpg --crop 0,0,1920,900

:: also crop the saved screenshots, not just detection
ss w1.mp4 slides jpg --crop 0,0,1920,900 --crop-output

:: limit to a time range and loosen the threshold
ss w1.mp4 slides jpg --start 00:01:30 --end 00:42:00 --threshold 0.90
```

`--crop X,Y,W,H` is in pixels of the original video, origin top-left. Put the
output folder and format before the first `-flag` or they are read as flags.

Browser/direct MP4 link example:

```cmd
cd /d D:\Project\Personal\video-slide-extractor
ss "https://example.com/video.mp4"
```

The MP4 link is downloaded into `input\downloads\`, then screenshots are extracted.

The extracted screenshots are saved in `slides\`.

Browser / H5P (Moodle) fallback — the `bs` command:

```cmd
cd /d D:\Project\Personal\video-slide-extractor

:: capture a lesson into browser-slides\
bs "https://online.codl.lk/mod/hvp/embed.php?id=112707"

:: bare word -> browser-slides\w5 ; full path -> used as-is ; png instead of jpg
bs "https://online.codl.lk/mod/hvp/embed.php?id=112707" w5 png

:: any -flag (and everything after) is passed straight to capture_browser_video.py
bs "https://online.codl.lk/mod/hvp/embed.php?id=112707" w5 --headless
```

`bs` wraps `capture_browser_video.py` with defaults tuned for lecture slides
(`--playback-rate 5`, `-r 3`, `--stability-duration 0.5`, `--min-slide-duration 1`,
`--duration 0`, `--manual-delay 0`, JPG). Argument order is
`bs <url> [folder|path] [png|jpg] [extra flags...]`.

Pass the H5P page URL, not the video file URL. The tool reads the page's
`H5PIntegration` data, finds the real MP4 behind the player and captures that
directly, so no player chrome or progress bar ends up in the screenshots.
Capture stops on its own when the video ends.

#### session.cmd — save the cookie and URL once

`bs` calls `session.cmd` (gitignored) at startup if it exists. Put the login
cookie there so it is never retyped, and optionally a default URL used when `bs`
is run with no URL argument:

```cmd
set "MOODLE_SESSION=your_cookie_here"
set "VIDEO_URL=https://online.codl.lk/mod/hvp/embed.php?id=112707"
```

With `session.cmd` in place, `bs` with no arguments captures `VIDEO_URL`.

#### What is the session ID?

`MOODLE_SESSION` is the value of the **`MoodleSession` cookie** from your
browser. It is what proves to the server that you are logged in. Without it a
protected lesson returns *"You do not have access to this content"* and nothing
is captured.

To get it in Chrome or Edge:

1. Log in to the Moodle site normally.
2. Press `F12` to open DevTools.
3. Go to **Application** -> **Storage** -> **Cookies** -> the site's address.
4. Find the row named `MoodleSession` and copy its **Value**
   (a long string such as `m0z5a6n4umqt1t79imvd99spvi`).

Treat that value like a password: anyone holding it is logged in as you. It
expires when you log out, so a new one is needed after logging out. Prefer the
environment variable over `--session`, which leaves the cookie in your command
history.

Check that the login works before a long capture - this prints the MP4 URL the
page is hiding, and nothing else:

```cmd
.venv\Scripts\python.exe capture_browser_video.py "https://online.codl.lk/mod/hvp/embed.php?id=112707" --print-video-url
```

#### Useful flags

| Flag | Meaning |
| --- | --- |
| `--playback-rate 4` | Play at 4x while capturing, so a 40-minute lecture takes about 10 minutes. Above 4, raise `-r` as well or short slides can be missed. |
| `--duration 0` | Capture until the video ends or `Ctrl + C` (the default). |
| `--manual-delay 0` | Start capturing immediately. The default is 20 seconds, which only exists so you can log in or press Play by hand; with `MOODLE_SESSION` set there is nothing to click, so 0 just saves the wait. Raise it (`--manual-delay 90`) when you do need to log in inside the window. |
| `--session VALUE` | Pass the cookie inline instead of via `MOODLE_SESSION`. |
| `--print-video-url` | Resolve the page to its MP4 URL, print it and exit. |
| `--no-resolve` | Screenshot the page itself instead of the MP4 behind it. |
| `--headless` | Run without a visible browser window. |
| `--click-center` | Click the middle of the player after the delay, for players that need a click to start. |

Best quality is still download-then-extract, because it decodes real video
frames instead of screenshots:

```cmd
.venv\Scripts\python.exe capture_browser_video.py "https://online.codl.lk/mod/hvp/embed.php?id=112707" --print-video-url > url.txt
set /p VURL=<url.txt
curl -L -o input\week2.mp4 -H "Cookie: MoodleSession=%MOODLE_SESSION%" "%VURL%"
ss week2.mp4 slides jpg
```

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


## License

Released under the [MIT License](LICENSE) — Copyright (c) 2026 Sandaruwan Weerawardhana.

Free to use, copy, modify, and distribute, provided the copyright and license
notice are kept. The software is provided "as is", without warranty of any kind.

