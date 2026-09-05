#!/usr/bin/env python3
"""
Capture slide screenshots from a browser-only video player.

This is the fallback path for H5P/Moodle embeds and other players that do not
expose a direct MP4 URL. It captures the rendered iframe/page pixels with
Playwright, then reuses the normal slide detector to keep only stable slides.
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "OpenCV and NumPy are not installed.\n"
        "Install the project dependencies first:\n"
        "    .venv\\Scripts\\python.exe -m pip install -r requirements.txt\n"
    )
    raise SystemExit(1)

import extract_slides as slides
from config import Config, ConfigError
from detector import SlideDetector
from utils import OutputError, SlideExtractorError, ensure_directory, setup_logging


DEFAULT_H5P_URL = "https://online.codl.lk/mod/hvp/embed.php?id=112707"
DEFAULT_OUTPUT_DIR = "browser-slides"
DEFAULT_SESSION_ENV = "MOODLE_SESSION"

#: Extensions a browser can play natively in a <video> element. A URL pointing
#: straight at one of these is NOT an embed page: putting it in an iframe gets
#: a bare download stub, so it needs a real <video> wrapper instead.
DIRECT_MEDIA_SUFFIXES = (".mp4", ".m4v", ".webm", ".ogv", ".ogg", ".mov", ".m3u8")

#: Text that means the page loaded but is refusing to show the video. Captured
#: as a "slide" by earlier versions, which then reported success.
ACCESS_ERROR_PATTERNS = (
    "you do not have access",
    "try logging in",
    "you are not logged in",
    "invalid login",
    "access denied",
    "permission denied",
    "sorry, this content is not available",
    "error/nopermission",
)


class IframeSnippetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.iframe_attrs: dict[str, str] = {}
        self.script_srcs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "iframe" and not self.iframe_attrs:
            self.iframe_attrs = values
        elif tag.lower() == "script" and values.get("src"):
            self.script_srcs.append(values["src"])


@dataclass
class EmbedSpec:
    src: str
    width: int
    height: int
    title: str
    scripts: list[str]
    #: "page" = navigate to it directly, "media" = wrap in a <video> element,
    #: "snippet" = iframe HTML was supplied, so a wrapper page is required.
    kind: str = "page"


@dataclass
class BrowserVideoInfo:
    path: Path
    duration: float
    width: int
    height: int

    def describe(self) -> str:
        duration = "until Ctrl+C" if self.duration <= 0 else slides.format_duration(self.duration)
        return (
            f"Source:        {self.path.name}\n"
            f"Capture size:  {self.width}x{self.height}\n"
            f"Duration:      {duration}"
        )


def positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return number


def non_negative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return number


def parse_viewport(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)\s*[x,]\s*(\d+)\s*", value)
    if not match:
        raise argparse.ArgumentTypeError("use WIDTHxHEIGHT, for example 1280x900")
    width, height = int(match.group(1)), int(match.group(2))
    if width < 320 or height < 240:
        raise argparse.ArgumentTypeError("viewport is too small")
    return width, height


def parse_embed_source(source: str) -> EmbedSpec:
    source = source.strip()
    if source.startswith("<"):
        parser = IframeSnippetParser()
        parser.feed(source)
        if not parser.iframe_attrs.get("src"):
            raise ValueError("iframe HTML does not contain an iframe src")
        return EmbedSpec(
            src=parser.iframe_attrs["src"],
            width=_int_attr(parser.iframe_attrs.get("width"), 1280),
            height=_int_attr(parser.iframe_attrs.get("height"), 720),
            title=parser.iframe_attrs.get("title") or "Browser video",
            scripts=parser.script_srcs,
            kind="snippet",
        )

    parsed = urllib.parse.urlparse(source)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("source must be an http(s) URL or an iframe HTML snippet")

    # A link straight to a media file is not an embed page. Rendering it in an
    # iframe only produces Chrome's download stub, so it gets a <video> wrapper.
    if Path(urllib.parse.unquote(parsed.path)).suffix.lower() in DIRECT_MEDIA_SUFFIXES:
        return EmbedSpec(
            src=source, width=1280, height=720,
            title="Direct video", scripts=[], kind="media",
        )

    scripts: list[str] = []
    if "/mod/hvp/" in parsed.path:
        scripts.append(f"{parsed.scheme}://{parsed.netloc}/mod/hvp/library/js/h5p-resizer.js")

    return EmbedSpec(
        src=source, width=1280, height=720,
        title="Browser video", scripts=scripts, kind="page",
    )


def _int_attr(value: Optional[str], fallback: int) -> int:
    """iframe width/height may be "100%", empty or junk; never crash on it."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def build_wrapper_html(embed: EmbedSpec) -> str:
    script_tags = "\n".join(
        f'<script src="{html.escape(src, quote=True)}" charset="UTF-8"></script>'
        for src in embed.scripts
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(embed.title)}</title>
  <style>
    html, body {{
      margin: 0;
      width: 100%;
      min-height: 100%;
      background: #111;
      overflow: hidden;
    }}
    #h5p-frame {{
      display: block;
      width: 100vw;
      height: 100vh;
      border: 0;
      background: #000;
    }}
  </style>
</head>
<body>
  <iframe
    id="h5p-frame"
    src="{html.escape(embed.src, quote=True)}"
    width="{embed.width}"
    height="{embed.height}"
    frameborder="0"
    allowfullscreen="allowfullscreen"
    allow="autoplay *; fullscreen *; encrypted-media *"
    title="{html.escape(embed.title, quote=True)}"></iframe>
  {script_tags}
</body>
</html>
"""


def build_media_html(embed: EmbedSpec) -> str:
    """Wrapper page for a direct media URL: a real <video>, not an iframe."""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{html.escape(embed.title)}</title>
  <style>
    html, body {{ margin: 0; width: 100%; height: 100%; background: #000; overflow: hidden; }}
    #player {{ display: block; width: 100vw; height: 100vh; background: #000; object-fit: contain; }}
  </style>
</head>
<body>
  <!-- No `controls`: the native bar carries a clock that ticks every second,
       which the detector would see as permanent motion. -->
  <video id="player" src="{html.escape(embed.src, quote=True)}"
         autoplay muted playsinline preload="auto"></video>
  <script>
    // Autoplay is only reliable while muted; unmute once playback has begun so
    // a visible run is watchable, and retry play() if the policy blocked it.
    const v = document.getElementById('player');
    v.play().catch(() => {{}});
    v.addEventListener('playing', () => {{ v.muted = false; }}, {{ once: true }});
  </script>
</body>
</html>
"""


def decode_screenshot(data: bytes):
    frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise OutputError("Browser screenshot could not be decoded")
    return frame


def resolve_h5p_video(url: str, session_id: str = "") -> Optional[str]:
    """Return the real MP4 URL behind an H5P activity page, if there is one.

    An H5P page embeds its media as a relative ``path`` inside the
    ``H5PIntegration`` JSON blob, joined to that content's ``contentUrl``.
    Pulling it out means the video can be captured (or downloaded) directly
    instead of screenshotting a player.
    """
    import json
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            **({"Cookie": f"MoodleSession={session_id}"} if session_id else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            page = response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError):
        return None

    match = re.search(r"H5PIntegration\s*=\s*(\{.*?\});\s*\n", page, re.S)
    if not match:
        return None
    try:
        integration = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None

    for content in (integration.get("contents") or {}).values():
        base = content.get("contentUrl")
        if not base:
            continue
        # jsonContent is itself a JSON *string*, so it needs a second decode.
        try:
            params = json.loads(content.get("jsonContent") or "{}")
        except json.JSONDecodeError:
            continue
        path = _first_video_path(params)
        if path:
            if path.startswith(("http://", "https://")):
                return path
            return f"{base.rstrip('/')}/{path.lstrip('/')}"
    return None


def _first_video_path(node) -> Optional[str]:
    """Depth-first hunt for the first video ``sources`` entry in H5P params."""
    if isinstance(node, dict):
        sources = node.get("sources")
        if isinstance(sources, list):
            for source in sources:
                if isinstance(source, dict) and source.get("path"):
                    mime = str(source.get("mime") or "")
                    path = str(source["path"])
                    if mime.startswith("video/") or Path(path).suffix.lower() in DIRECT_MEDIA_SUFFIXES:
                        return path
        for value in node.values():
            found = _first_video_path(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _first_video_path(value)
            if found:
                return found
    return None


def load_playwright():
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.stderr.write(
            "Playwright is not installed.\n"
            "Install the browser-capture dependency once:\n"
            "    .venv\\Scripts\\python.exe -m pip install playwright\n"
            "    .venv\\Scripts\\python.exe -m playwright install chromium\n"
        )
        raise SystemExit(1)
    return sync_playwright, PlaywrightError


def session_from_args(args: argparse.Namespace) -> str:
    """Cookie value from --session, else from the --session-env variable."""
    if getattr(args, "session", None):
        return str(args.session).strip()
    return os.environ.get(args.session_env, "").strip() if args.session_env else ""


def add_moodle_session_cookie(
    context, embed: EmbedSpec, env_name: str, quiet: bool = False, session_id: str = ""
) -> bool:
    """Inject a MoodleSession cookie. Returns True if one was set."""
    session_id = (session_id or os.environ.get(env_name, "")).strip()
    if not session_id:
        if not quiet and env_name:
            print(
                f"No {env_name} cookie in the environment; relying on the browser "
                "profile for login."
            )
        return False

    parsed = urllib.parse.urlparse(embed.src)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("cannot set Moodle session cookie for this source URL")

    context.add_cookies([
        {
            "name": "MoodleSession",
            "value": session_id,
            "domain": parsed.hostname,
            "path": "/",
            "httpOnly": True,
            "secure": parsed.scheme == "https",
            "sameSite": "Lax",
        }
    ])
    return True


def frame_is_blank(frame, tolerance: float = 3.0) -> bool:
    """True when a screenshot carries essentially no structure.

    A dead iframe, a black player and a not-yet-painted page all come back as a
    near-uniform rectangle. Committing one as slide 1 is what made a completely
    failed capture look successful, so it is checked before anything is saved.
    """
    if frame is None or frame.size == 0:
        return True
    grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    return float(np.std(grey.astype(np.float32))) < tolerance


def find_access_error(page) -> Optional[str]:
    """Return the matching access-error phrase visible on the page, if any.

    Looks at the top document and every child frame, because the Moodle/H5P
    "you do not have access" notice is rendered inside the embed frame.
    """
    for frame in page.frames:
        try:
            text = (frame.inner_text("body") or "").strip().lower()
        except Exception:
            # Cross-origin or already-detached frames simply cannot be read.
            continue
        # Only short pages are error stubs; a real player page has far more text.
        if len(text) > 4000:
            continue
        for pattern in ACCESS_ERROR_PATTERNS:
            if pattern in text:
                return pattern
    return None


#: HTMLMediaElement.error codes, which are otherwise reported to the user as an
#: unexplained blank capture.
MEDIA_ERROR_TEXT = {
    1: "loading was aborted",
    2: "a network error occurred (the URL may have expired or need a login)",
    3: "the video could not be decoded",
    4: "the source is not supported (a 403/404 response, or an unplayable format)",
}


def watch_media_responses(page, embed: EmbedSpec) -> dict:
    """Record the HTTP status of the media request itself.

    A 403/404 on the video file is the most common reason for a black player,
    and the <video> element only reports it as a generic "src not supported".
    """
    seen: dict = {}

    def on_response(response) -> None:
        if response.url == embed.src and "status" not in seen:
            seen["status"] = response.status
            seen["url"] = response.url

    page.on("response", on_response)
    return seen


def find_media_error(page) -> Optional[str]:
    """Return a readable <video> error from any frame, if one failed."""
    for frame in page.frames:
        try:
            code = frame.evaluate(
                """() => {
                    for (const v of document.querySelectorAll('video')) {
                        if (v.error) return v.error.code;
                    }
                    return null;
                }"""
            )
        except Exception:
            continue
        if code:
            return MEDIA_ERROR_TEXT.get(int(code), f"media error code {code}")
    return None


def start_playback(page, rate: float = 1.0) -> None:
    """Force play() on every <video>, in the top document and in child frames.

    Autoplay is blocked unless the video is muted, and a paused player just
    shows its poster frame forever - which is exactly one "stable slide" and
    nothing else. Muting first is what actually gets playback going.

    ``rate`` speeds playback up so a long lecture is captured in a fraction of
    its running time. It is re-applied periodically during capture because
    players commonly reset playbackRate when they buffer or change source.
    """
    script = """(rate) => {
        for (const v of document.querySelectorAll('video')) {
            try {
                // Never restart a finished video: this runs periodically to
                // re-apply the speed-up, and calling play() on an ended
                // player rewinds it, so the capture would loop forever.
                if (v.ended) continue;
                v.muted = true;
                if (rate && v.playbackRate !== rate) v.playbackRate = rate;
                const p = v.play(); if (p) p.catch(() => {});
            } catch (e) {}
        }
    }"""
    for frame in page.frames:
        try:
            frame.evaluate(script, rate)
        except Exception:
            # Cross-origin frames cannot be scripted; --click-center covers those.
            continue


def playback_finished(page) -> bool:
    """True once a video that really played has reached its end.

    Guarded on currentTime > 0 so a player that has not started yet - which
    also reports ended === false - is never mistaken for a finished one.
    """
    for frame in page.frames:
        try:
            done = frame.evaluate(
                """() => {
                    const vs = Array.from(document.querySelectorAll('video'));
                    if (vs.length === 0) return false;
                    return vs.some(v => v.ended && v.currentTime > 0);
                }"""
            )
        except Exception:
            continue
        if done:
            return True
    return False


def wait_for_media(page, timeout_ms: int = 15000, rate: float = 1.0) -> None:
    """Wait until a player is actually advancing, not merely loaded."""
    start_playback(page, rate)
    try:
        # currentTime > 0 is the real signal: readyState alone is satisfied by a
        # paused video sitting on its poster frame.
        page.wait_for_function(
            """() => {
                const vs = Array.from(document.querySelectorAll('video'));
                if (vs.length === 0) return false;
                return vs.some(v => v.currentTime > 0 && !v.paused);
            }""",
            timeout=timeout_ms,
        )
    except Exception:
        # Not fatal: many players only create the <video> after a user click,
        # and the blank-frame guard still catches a genuinely dead capture.
        pass


#: Tried in order when narrowing a navigated page down to just the player.
#: Screenshotting the whole page would drag in site chrome and navigation, and
#: any of it that animates keeps the detector from ever settling.
PLAYER_SELECTORS = (
    ".h5p-video video",
    ".h5p-iframe-wrapper",
    "iframe.h5p-iframe",
    "video",
    "iframe",
)


def resolve_page_target(page):
    """Narrow a navigated page to the player element, or None for full page."""
    for selector in PLAYER_SELECTORS:
        locator = page.locator(selector).first
        try:
            if locator.count() == 0:
                continue
            box = locator.bounding_box()
        except Exception:
            continue
        # Ignore tracking pixels and collapsed placeholders.
        if box and box["width"] >= 320 and box["height"] >= 240:
            return locator
    return None


def is_browser_closed_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "target page, context or browser has been closed" in message
        or "browser has been closed" in message
        or "page has been closed" in message
        # A crashed tab (out of memory, GPU fault) is equally unrecoverable,
        # but the slides already written to disk are still good and must be
        # kept rather than discarded with an exception.
        or "target crashed" in message
        or "page crashed" in message
    )


def interrupted_result(output_dir: Path, writer=None, detector=None) -> slides.Result:
    return slides.Result(
        slides=len(writer.slides) if writer is not None else 0,
        samples=detector.stats.samples if detector is not None else 0,
        duplicates=detector.stats.duplicates_skipped if detector is not None else 0,
        builds=detector.stats.builds_merged if detector is not None else 0,
        aborted=detector.stats.transitions_aborted if detector is not None else 0,
        interrupted=True,
        output_dir=output_dir,
    )


def build_parser() -> argparse.ArgumentParser:
    defaults = Config()
    parser = argparse.ArgumentParser(
        prog="capture_browser_video.py",
        description="Capture stable slide screenshots from a browser iframe/video player.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
examples:
  python capture_browser_video.py "{DEFAULT_H5P_URL}" --output browser-slides --duration 1800
  python capture_browser_video.py "{DEFAULT_H5P_URL}" --manual-delay 45 --duration 0

notes:
  Use the iframe src URL when possible. Passing full iframe HTML also works, but
  command-line quoting is easier with the plain src URL.
  --duration 0 keeps capturing until Ctrl+C.
""",
    )
    parser.add_argument(
        "source",
        nargs="?",
        default=DEFAULT_H5P_URL,
        help="iframe src URL or full iframe HTML snippet",
    )
    parser.add_argument(
        "-o", "--output", nargs="?", const=DEFAULT_OUTPUT_DIR, default=DEFAULT_OUTPUT_DIR,
        metavar="DIR",
        help=(
            "directory for slide screenshots; omit DIR to use "
            f"{DEFAULT_OUTPUT_DIR} (default: {DEFAULT_OUTPUT_DIR})"
        ),
    )
    parser.add_argument(
        "-r", "--sample-rate", type=positive_float, default=1.0, metavar="FPS",
        help="browser screenshots analysed per second (default: 1)",
    )
    parser.add_argument(
        "--duration", type=non_negative_float, default=0.0, metavar="SECONDS",
        help="capture length; 0 means until Ctrl+C (default: 0)",
    )
    parser.add_argument(
        "--manual-delay", type=non_negative_float, default=20.0, metavar="SECONDS",
        help="time to log in or press Play before capture starts (default: 20)",
    )
    parser.add_argument(
        "--viewport", type=parse_viewport, default=(1280, 900), metavar="WIDTHxHEIGHT",
        help="browser viewport size (default: 1280x900)",
    )
    parser.add_argument(
        "-f", "--format", choices=("png", "jpg"), default=defaults.output_format,
        help=f"saved slide format (default: {defaults.output_format})",
    )
    parser.add_argument(
        "--threshold", type=float, default=defaults.change_threshold,
        help=f"slide-change threshold (default: {defaults.change_threshold})",
    )
    parser.add_argument(
        "--stability-threshold", type=float, default=defaults.stability_threshold,
        help=f"stable-frame threshold (default: {defaults.stability_threshold})",
    )
    parser.add_argument(
        "--stability-duration", type=float, default=defaults.stability_duration,
        help=f"seconds the picture must stay still (default: {defaults.stability_duration})",
    )
    parser.add_argument(
        "--min-slide-duration", type=float, default=defaults.min_slide_duration,
        help=f"merge changes faster than this into one slide (default: {defaults.min_slide_duration})",
    )
    parser.add_argument(
        "--duplicate-threshold", type=float, default=defaults.duplicate_threshold,
        help=f"skip near-identical slides above this similarity (default: {defaults.duplicate_threshold})",
    )
    parser.add_argument(
        "--jpeg-quality", type=int, default=defaults.jpeg_quality,
        help=f"JPEG quality when --format jpg (default: {defaults.jpeg_quality})",
    )
    parser.add_argument(
        "--user-data-dir", default="browser-profile",
        help="persistent browser profile for Moodle/login cookies (default: browser-profile)",
    )
    parser.add_argument(
        "--session-env", default=DEFAULT_SESSION_ENV, metavar="NAME",
        help=(
            "environment variable containing a MoodleSession cookie "
            f"(default: {DEFAULT_SESSION_ENV}; unset means no cookie injection)"
        ),
    )
    parser.add_argument(
        "--session", default=None, metavar="COOKIE",
        help=(
            "MoodleSession cookie value; prefer the environment variable named "
            "by --session-env so the cookie stays out of your shell history"
        ),
    )
    parser.add_argument(
        "--no-resolve", action="store_true",
        help="do not look inside an H5P page for the underlying video URL",
    )
    parser.add_argument(
        "--print-video-url", action="store_true",
        help="resolve the H5P page to its MP4 URL, print it and exit",
    )
    parser.add_argument(
        "--channel", default=None,
        help="optional browser channel, for example chrome or msedge",
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="run without a visible browser window",
    )
    parser.add_argument(
        "--click-center", action="store_true",
        help="click the middle of the iframe after the manual delay",
    )
    parser.add_argument(
        "--playback-rate", type=positive_float, default=1.0, metavar="RATE",
        help=(
            "speed the video up while capturing, for example 2 or 4 "
            "(default: 1). Above 4 most browsers stop rendering every frame, "
            "so short slides can be missed"
        ),
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true",
        help="only warnings and errors",
    )
    return parser


def config_from_args(args: argparse.Namespace) -> Config:
    config = Config(
        sample_rate=args.sample_rate,
        change_threshold=args.threshold,
        stability_threshold=args.stability_threshold,
        stability_duration=args.stability_duration,
        min_slide_duration=args.min_slide_duration,
        duplicate_threshold=args.duplicate_threshold,
        output_dir=args.output,
        output_format=args.format,
        jpeg_quality=args.jpeg_quality,
        quiet=args.quiet,
    )
    if config.stability_threshold < config.change_threshold:
        config.stability_threshold = min(0.999, (1.0 + config.change_threshold) / 2)
    if config.duplicate_threshold < config.change_threshold:
        config.duplicate_threshold = config.change_threshold
    config.validate()
    return config


def capture(args: argparse.Namespace, config: Config) -> slides.Result:
    sync_playwright, PlaywrightError = load_playwright()
    embed = parse_embed_source(args.source)
    session_id = session_from_args(args)

    # An H5P activity page hides a plain MP4 behind its player. Capturing that
    # file directly avoids screenshotting player chrome, and it is what makes
    # the video reachable at all when the player itself needs a login.
    if embed.kind == "page" and not args.no_resolve and "/mod/hvp/" in embed.src:
        resolved = resolve_h5p_video(embed.src, session_id)
        if resolved:
            if not args.quiet:
                print(f"Resolved H5P video: {resolved}")
            embed = parse_embed_source(resolved)

    output_dir = ensure_directory(Path(config.output_dir), purpose="output")
    extract_slides_log = setup_logging(debug=False, quiet=args.quiet)
    slides.log = extract_slides_log

    with sync_playwright() as playwright:
        launch_args = ["--autoplay-policy=no-user-gesture-required"]
        user_data_dir = Path(args.user_data_dir) if args.user_data_dir else None
        if user_data_dir:
            context = playwright.chromium.launch_persistent_context(
                str(user_data_dir),
                headless=args.headless,
                viewport={"width": args.viewport[0], "height": args.viewport[1]},
                channel=args.channel,
                args=launch_args,
            )
            browser = None
            page = context.pages[0] if context.pages else context.new_page()
        else:
            browser = playwright.chromium.launch(
                headless=args.headless,
                channel=args.channel,
                args=launch_args,
            )
            context = browser.new_context(
                viewport={"width": args.viewport[0], "height": args.viewport[1]}
            )
            page = context.new_page()

        try:
            add_moodle_session_cookie(
                context, embed, args.session_env, quiet=args.quiet, session_id=session_id
            )

            # Navigating to the embed URL directly (rather than dropping it into
            # a locally generated wrapper page) is what makes login work at all:
            # a wrapper leaves the page on about:blank, so the browser profile's
            # cookies never apply to it and there is no page to log in on.
            media_response = watch_media_responses(page, embed)

            if embed.kind == "media":
                # Serve the wrapper from the video's own origin instead of
                # set_content(). A set_content() page stays on about:blank, so
                # the media request carries no cookies and no Referer, and a
                # login-protected or hotlink-protected file comes back 403 -
                # a black player with no explanation.
                wrapper_url = urllib.parse.urljoin(embed.src, "__slide_capture__.html")
                wrapper_body = build_media_html(embed)
                page.route(
                    wrapper_url,
                    lambda route: route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body=wrapper_body,
                    ),
                )
                page.goto(wrapper_url, wait_until="domcontentloaded", timeout=60000)
                target = page.locator("#player")
            elif embed.kind == "snippet":
                page.set_content(build_wrapper_html(embed), wait_until="domcontentloaded")
                target = page.locator("#h5p-frame")
            else:
                page.goto(embed.src, wait_until="domcontentloaded", timeout=60000)
                target = None  # whole page; narrowed below once the player exists

            if target is not None:
                target.wait_for(state="visible", timeout=30000)

            problem = find_access_error(page)
            if problem and not args.quiet:
                print(
                    f"Warning: the page says {problem!r}.\n"
                    "         Log in inside the browser window now - the capture "
                    "will re-check before it starts."
                )

            if args.manual_delay and not args.quiet:
                print(
                    f"Browser is open. Log in or press Play if needed; "
                    f"capture starts in {args.manual_delay:g} seconds."
                )
            page.wait_for_timeout(args.manual_delay * 1000)

            # The manual delay is there so the user can log in, which usually
            # means the page navigated. Re-resolve the target afterwards.
            if embed.kind == "page":
                target = resolve_page_target(page)

            if args.click_center and target is not None:
                box = target.bounding_box()
                if box:
                    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                    page.wait_for_timeout(500)

            wait_for_media(page, rate=args.playback_rate)

            problem = find_access_error(page)
            if problem:
                raise OutputError(
                    f"The player page is showing an access error ({problem!r}).\n"
                    "Nothing was captured because the video never became visible.\n"
                    "Log in to the site in the browser window during --manual-delay "
                    "(raise it, for example --manual-delay 90), or export a session "
                    f"cookie in {args.session_env}."
                )

            shooter = (lambda: target.screenshot(type="png")) if target is not None \
                else (lambda: page.screenshot(type="png"))

            # A video legitimately starts on a black frame (fade-in, title card),
            # so one blank screenshot proves nothing. Only a capture area that
            # stays blank for several seconds is a genuinely dead player.
            first_frame = decode_screenshot(shooter())
            blank_deadline = time.monotonic() + 10.0
            while frame_is_blank(first_frame) and time.monotonic() < blank_deadline:
                page.wait_for_timeout(500)
                first_frame = decode_screenshot(shooter())
            if frame_is_blank(first_frame):
                status = media_response.get("status")
                media_error = find_media_error(page)
                if status and status >= 400:
                    raise OutputError(
                        f"The server refused the video with HTTP {status}.\n"
                        f"Source: {embed.src}\n"
                        "This link needs a login or has expired. Capture the lesson "
                        "page the video is embedded on instead of the file URL, so "
                        "the browser can send your session."
                    )
                if media_error:
                    raise OutputError(
                        f"The video failed to load: {media_error}.\n"
                        f"Source: {embed.src}\n"
                        "Check the URL in a normal browser; if it needs a login, "
                        "capture the page it is embedded on instead of the file URL."
                    )
                raise OutputError(
                    "The capture area stayed blank - the player never rendered.\n"
                    "Check that the video is visible and playing in the browser "
                    "window, increase --manual-delay, or pass --click-center to "
                    "start playback."
                )
            height, width = first_frame.shape[:2]
            source_name = Path(urllib.parse.urlparse(embed.src).path).name or "browser-video"
            info = BrowserVideoInfo(
                path=Path(source_name),
                duration=args.duration,
                width=width,
                height=height,
            )
            if not args.quiet:
                print("Processing browser video...")
                print(info.describe())
                print()

            detector = SlideDetector(config, (width, height))
            writer = slides.SlideWriter(config, output_dir, source_name)
            result = slides.Result(output_dir=output_dir)
            interval = 1.0 / config.sample_rate
            started = time.monotonic()
            next_capture = started
            frame_index = 0
            blanks_skipped = 0

            while True:
                now = time.monotonic()
                timestamp = now - started
                if args.duration > 0 and timestamp >= args.duration:
                    break

                if now < next_capture:
                    time.sleep(min(next_capture - now, 0.25))
                    continue

                frame = decode_screenshot(shooter())
                events = detector.process(timestamp, frame_index, frame)
                for event in events:
                    # A blank frame is a stalled or unloaded player, not a
                    # slide. It is the most "stable" picture there is, so
                    # without this it would be committed and saved.
                    if frame_is_blank(event.frame):
                        blanks_skipped += 1
                        continue
                    writer.add(event, detector)

                frame_index += 1
                next_capture += interval
                # A screenshot can take longer than one interval. Without this
                # the schedule falls permanently behind the clock and the loop
                # spins at full speed instead of at --sample-rate.
                if next_capture < now:
                    next_capture = now + interval

                # Re-assert the speed-up: players reset playbackRate whenever
                # they buffer or switch source, which would silently drop the
                # capture back to 1x partway through a long lecture.
                if args.playback_rate != 1.0 and frame_index % 10 == 0:
                    start_playback(page, args.playback_rate)

                # A sped-up video finishes well before the wall-clock duration.
                # Without this the loop keeps screenshotting a finished player.
                if frame_index % 5 == 0 and playback_finished(page):
                    if not args.quiet:
                        print("Video reached the end; stopping capture.")
                    break

            for event in detector.finalize():
                if frame_is_blank(event.frame):
                    blanks_skipped += 1
                    continue
                writer.add(event, detector)

            if blanks_skipped and not args.quiet:
                print(f"Skipped {blanks_skipped} blank frame(s) from a stalled player.")

            writer.write_metadata(info, config)
            result.slides = len(writer.slides)
            result.samples = detector.stats.samples
            result.duplicates = detector.stats.duplicates_skipped
            result.builds = detector.stats.builds_merged
            result.aborted = detector.stats.transitions_aborted
            return result
        except KeyboardInterrupt:
            if "writer" in locals() and "info" in locals():
                writer.write_metadata(info, config, partial=True)
                return interrupted_result(output_dir, writer, detector if "detector" in locals() else None)
            raise
        except PlaywrightError as exc:
            if is_browser_closed_error(exc):
                if "writer" in locals() and "info" in locals():
                    writer.write_metadata(info, config, partial=True)
                    return interrupted_result(
                        output_dir,
                        writer,
                        detector if "detector" in locals() else None,
                    )
                return interrupted_result(output_dir)
            raise OutputError(f"Browser capture failed: {exc}") from exc
        finally:
            # The browser is often already gone by the time we get here (the
            # user closed the window). Closing it again raises and would
            # replace the real exception with a useless one.
            for closeable in (context, browser):
                if closeable is None:
                    continue
                try:
                    closeable.close()
                except Exception:
                    pass


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if args.print_video_url:
        video_url = resolve_h5p_video(args.source, session_from_args(args))
        if not video_url:
            sys.stderr.write(
                "Could not find a video URL on that page.\n"
                "Check the URL, and pass --session (or set the environment "
                f"variable {args.session_env}) if the page needs a login.\n"
            )
            return 3
        print(video_url)
        return 0

    try:
        config = config_from_args(args)
        result = capture(args, config)
    except (ConfigError, ValueError) as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    except SlideExtractorError as exc:
        sys.stderr.write(f"\nError: {exc}\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("\nAborted.\n")
        return 130

    print()
    print("Capture complete." if not result.interrupted else "Capture interrupted.")
    print()
    print(f"Slides detected:  {result.slides}")
    print(f"Frames analysed:  {result.samples}")
    print(f"Output directory: {result.output_dir}")
    if result.slides:
        print(f"Metadata:         {result.output_dir / 'slides.json'}, {result.output_dir / 'slides.txt'}")
    if result.interrupted and result.slides == 0:
        sys.stderr.write("\nCapture stopped before any slides were detected.\n")
        return 130
    if result.slides == 0:
        sys.stderr.write(
            "\nNo slides were detected. Confirm the video was visible and playing in the browser.\n"
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
