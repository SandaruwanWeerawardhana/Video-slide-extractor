"""Small helpers shared by the rest of the project: time formatting, safe
image writing, logging setup and the terminal progress display."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Optional

import numpy as np


# --------------------------------------------------------------------- errors
class SlideExtractorError(Exception):
    """Base class for every error this tool reports to the user."""


class VideoError(SlideExtractorError):
    """The video cannot be opened, read or understood."""


class OutputError(SlideExtractorError):
    """The output directory or a screenshot cannot be written."""


# ------------------------------------------------------------------ time utils
def format_timestamp(seconds: float, millis: bool = True) -> str:
    """Format seconds as ``HH:MM:SS.mmm`` (or ``HH:MM:SS``)."""
    if seconds is None or seconds < 0 or not np.isfinite(seconds):
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    if millis:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_duration(seconds: float) -> str:
    return format_timestamp(seconds, millis=False)


# ---------------------------------------------------------------- image saving
def save_image(
    frame: np.ndarray,
    path: Path,
    jpeg_quality: int = 95,
    metadata: Optional[dict] = None,
) -> None:
    """Write a BGR frame to ``path``.

    ``cv2.imwrite`` cannot handle non-ASCII paths on Windows, so the image is
    always encoded in memory and written with plain Python file IO.

    When ``metadata`` is given and Pillow is installed, PNG files get the
    key/value pairs embedded as tEXt chunks, so a stray ``slide_042.png`` still
    knows which video and timestamp it came from.
    """
    import cv2  # imported here so a missing OpenCV is reported by main()

    suffix = path.suffix.lower()

    if metadata and suffix == ".png":
        try:
            from PIL import Image, PngImagePlugin

            info = PngImagePlugin.PngInfo()
            for key, value in metadata.items():
                info.add_text(str(key), str(value))
            image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            path.parent.mkdir(parents=True, exist_ok=True)
            image.save(path, format="PNG", pnginfo=info, compress_level=3)
            return
        except ImportError:
            pass  # Pillow is optional; fall through to the OpenCV writer.
        except PermissionError:
            raise OutputError(
                f"Permission denied writing {path}. "
                "Close any program that may have the file open, or pick another "
                "--output directory."
            )
        except OSError as exc:
            raise OutputError(f"Could not write {path}: {exc}")

    if suffix in (".jpg", ".jpeg"):
        params = [cv2.IMWRITE_JPEG_QUALITY, int(jpeg_quality)]
    elif suffix == ".png":
        # 3 = good speed/size compromise; slides compress well anyway.
        params = [cv2.IMWRITE_PNG_COMPRESSION, 3]
    else:
        params = []

    ok, buffer = cv2.imencode(suffix, frame, params)
    if not ok:
        raise OutputError(f"Failed to encode image for {path}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(buffer.tobytes())
    except PermissionError:
        raise OutputError(
            f"Permission denied writing {path}. "
            "Close any program that may have the file open, or pick another "
            "--output directory."
        )
    except OSError as exc:
        raise OutputError(f"Could not write {path}: {exc}")


def ensure_directory(path: Path, purpose: str = "output") -> Path:
    """Create ``path`` if needed and verify it is a writable directory."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        raise OutputError(f"The {purpose} path {path} exists but is not a directory")
    except PermissionError:
        raise OutputError(f"Permission denied creating {purpose} directory {path}")
    except OSError as exc:
        raise OutputError(f"Could not create {purpose} directory {path}: {exc}")

    if not path.is_dir():
        raise OutputError(f"The {purpose} path {path} is not a directory")

    probe = path / ".write_test"
    try:
        probe.write_bytes(b"")
        probe.unlink()
    except OSError:
        raise OutputError(f"The {purpose} directory {path} is not writable")
    return path


# --------------------------------------------------------------------- logging
def setup_logging(debug: bool = False, quiet: bool = False) -> logging.Logger:
    logger = logging.getLogger("slides")
    logger.handlers.clear()
    logger.propagate = False

    # stdout, so log lines and the progress display stay in the right order.
    handler = logging.StreamHandler(sys.stdout)
    if debug:
        level = logging.DEBUG
        fmt = "%(asctime)s %(levelname)-7s %(message)s"
    else:
        level = logging.WARNING if quiet else logging.INFO
        fmt = "%(message)s"
    handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
    handler.setLevel(level)
    logger.addHandler(handler)
    logger.setLevel(level)
    return logger


# -------------------------------------------------------------------- progress
class ProgressDisplay:
    """Single-line terminal progress indicator.

    Falls back to periodic plain lines when stdout is not a terminal (for
    example when the output is piped to a file).
    """

    def __init__(self, total_seconds: float, enabled: bool = True, interval: float = 0.2):
        self.total = max(total_seconds, 0.0)
        self.enabled = enabled
        self.interval = interval
        self.is_tty = sys.stdout.isatty()
        self._last_draw = 0.0
        self._started = time.monotonic()
        self._line_length = 0

    def update(self, current_seconds: float, slides: int, force: bool = False) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if not force and now - self._last_draw < self.interval:
            return
        self._last_draw = now

        percent = (current_seconds / self.total * 100.0) if self.total > 0 else 0.0
        percent = min(max(percent, 0.0), 100.0)
        elapsed = now - self._started
        speed = (current_seconds / elapsed) if elapsed > 0 else 0.0
        eta = ((self.total - current_seconds) / speed) if speed > 0.01 else 0.0

        line = (
            f"Progress: {percent:5.1f}%  "
            f"Current time: {format_duration(current_seconds)}  "
            f"Slides detected: {slides}  "
            f"Speed: {speed:4.1f}x  "
            f"ETA: {format_duration(eta)}"
        )
        if self.is_tty:
            padding = " " * max(0, self._line_length - len(line))
            sys.stdout.write("\r" + line + padding)
            sys.stdout.flush()
            self._line_length = len(line)
        elif force or now - self._started > 5:
            print(line, flush=True)

    def finish(self) -> None:
        if self.enabled and self.is_tty and self._line_length:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._line_length = 0


def parse_crop(text: str) -> tuple[int, int, int, int]:
    """Parse a ``x,y,width,height`` crop string."""
    parts = [p.strip() for p in text.replace(" ", "").split(",")]
    if len(parts) != 4:
        raise ValueError(
            f"Crop must be 'x,y,width,height' (4 numbers), got '{text}'"
        )
    try:
        x, y, w, h = (int(p) for p in parts)
    except ValueError:
        raise ValueError(f"Crop values must be whole numbers, got '{text}'")
    if x < 0 or y < 0 or w <= 0 or h <= 0:
        raise ValueError(
            f"Crop needs x>=0, y>=0, width>0, height>0, got '{text}'"
        )
    return x, y, w, h


def parse_time(text: str) -> float:
    """Parse ``90``, ``1:30`` or ``00:01:30.5`` into seconds."""
    text = text.strip()
    if not text:
        raise ValueError("Empty time value")
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(f"Invalid time '{text}'; use SS, MM:SS or HH:MM:SS")
    try:
        values = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"Invalid time '{text}'; use SS, MM:SS or HH:MM:SS")
    seconds = 0.0
    for value in values:
        seconds = seconds * 60 + value
    if seconds < 0:
        raise ValueError(f"Time cannot be negative: '{text}'")
    return seconds


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def clamp_crop(
    crop: tuple[int, int, int, int], width: int, height: int
) -> Optional[tuple[int, int, int, int]]:
    """Clip a crop rectangle to the frame; return None if it falls outside."""
    x, y, w, h = crop
    x2 = min(x + w, width)
    y2 = min(y + h, height)
    x = min(x, width)
    y = min(y, height)
    if x2 - x <= 0 or y2 - y <= 0:
        return None
    return x, y, x2 - x, y2 - y
