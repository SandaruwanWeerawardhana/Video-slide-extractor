"""Video access layer.

Wraps ``cv2.VideoCapture`` and hides the awkward parts: unreliable metadata,
frame timestamps, seeking and cheap frame skipping.

The video is always read sequentially and only one frame is held in memory at
a time, so a three hour 1080p lecture costs a few megabytes, not gigabytes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Tuple

import numpy as np

from utils import VideoError, format_duration, human_size

log = logging.getLogger("slides")

# Extensions OpenCV can normally read.  Anything else still gets a try, but
# the error message mentions the mismatch.
KNOWN_EXTENSIONS = {
    ".mp4", ".m4v", ".mkv", ".mov", ".avi", ".wmv", ".flv",
    ".webm", ".mpg", ".mpeg", ".ts", ".m2ts", ".ogv", ".3gp",
}


@dataclass
class VideoInfo:
    path: Path
    width: int
    height: int
    fps: float
    frame_count: int
    duration: float
    fps_estimated: bool = False
    duration_estimated: bool = False
    size_bytes: int = 0

    def describe(self) -> str:
        lines = [
            f"File:       {self.path.name}  ({human_size(self.size_bytes)})",
            f"Duration:   {format_duration(self.duration)}"
            + ("  (estimated)" if self.duration_estimated else ""),
            f"Resolution: {self.width}x{self.height}",
            f"FPS:        {self.fps:.2f}"
            + ("  (assumed)" if self.fps_estimated else ""),
            f"Frames:     {self.frame_count if self.frame_count > 0 else 'unknown'}",
        ]
        return "\n".join(lines)


class VideoSource:
    """Sequential, sampling reader over a local video file."""

    def __init__(self, path: str | Path):
        try:
            import cv2  # noqa: F401
        except ImportError:  # pragma: no cover - environment problem
            raise VideoError(
                "OpenCV is not installed. Install the dependencies with:\n"
                "    pip install -r requirements.txt"
            )
        self._cv2 = cv2

        self.path = Path(path).expanduser()
        self._validate_path()
        self.capture = self._open()
        self.info = self._probe()

    # ------------------------------------------------------------- lifecycle
    def _validate_path(self) -> None:
        if not self.path.exists():
            raise VideoError(f"Video file not found: {self.path}")
        if self.path.is_dir():
            raise VideoError(f"{self.path} is a directory, not a video file")
        try:
            size = self.path.stat().st_size
        except OSError as exc:
            raise VideoError(f"Cannot read {self.path}: {exc}")
        if size == 0:
            raise VideoError(f"Video file is empty (0 bytes): {self.path}")
        if self.path.suffix.lower() not in KNOWN_EXTENSIONS:
            log.warning(
                "Warning: '%s' is not a usual video extension; trying anyway.",
                self.path.suffix or "(no extension)",
            )

    def _open(self):
        cv2 = self._cv2
        capture = cv2.VideoCapture(str(self.path))
        if not capture.isOpened():
            raise VideoError(
                f"Could not open video: {self.path}\n"
                "The file may be corrupted, or the codec may not be supported by "
                "your OpenCV build. Try re-encoding it first:\n"
                f'    ffmpeg -i "{self.path.name}" -c:v libx264 -an fixed.mp4'
            )
        return capture

    def _probe(self) -> VideoInfo:
        cv2 = self._cv2
        cap = self.capture

        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

        fps_estimated = False
        if not np.isfinite(fps) or fps <= 0.1 or fps > 1000:
            log.warning("Video reports an implausible FPS (%s); assuming 25.", fps)
            fps = 25.0
            fps_estimated = True

        # Some containers lie about size/frame count. Read one frame to confirm.
        ok, frame = cap.read()
        if not ok or frame is None:
            raise VideoError(
                f"Could not decode any frame from {self.path}. "
                "The file is probably corrupted or uses an unsupported codec."
            )
        height, width = frame.shape[:2]
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

        duration_estimated = False
        if frame_count > 0:
            duration = frame_count / fps
        else:
            duration = 0.0
            duration_estimated = True
            log.warning(
                "Video does not report a frame count; progress will be approximate."
            )

        try:
            size_bytes = self.path.stat().st_size
        except OSError:
            size_bytes = 0

        return VideoInfo(
            path=self.path,
            width=width,
            height=height,
            fps=fps,
            frame_count=max(frame_count, 0),
            duration=duration,
            fps_estimated=fps_estimated,
            duration_estimated=duration_estimated,
            size_bytes=size_bytes,
        )

    def close(self) -> None:
        if getattr(self, "capture", None) is not None:
            self.capture.release()
            self.capture = None

    def __enter__(self) -> "VideoSource":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ---------------------------------------------------------------- reading
    def iter_samples(
        self,
        sample_rate: float,
        start: Optional[float] = None,
        end: Optional[float] = None,
    ) -> Iterator[Tuple[float, int, np.ndarray]]:
        """Yield ``(timestamp_seconds, frame_index, frame)`` at ~sample_rate fps.

        Frames between two samples are skipped with ``grab()``, which decodes
        as little as the container allows and is far cheaper than ``read()``.
        """
        cv2 = self._cv2
        cap = self.capture
        fps = self.info.fps

        step = max(1, int(round(fps / float(sample_rate))))
        log.debug(
            "Sampling every %d frame(s) (%.2f fps source, %.2f fps requested)",
            step, fps, sample_rate,
        )

        index = 0
        if start:
            index = int(start * fps)
            if not cap.set(cv2.CAP_PROP_POS_FRAMES, index):
                log.warning("Seeking to %s failed; starting from 0.", format_duration(start))
                index = 0

        end_index = int(end * fps) if end else None
        use_msec = True
        last_msec = -1.0
        consecutive_failures = 0

        while True:
            if end_index is not None and index > end_index:
                break

            # Position query must happen *before* read(): it reports the
            # timestamp of the frame that is about to be decoded.
            timestamp = index / fps
            if use_msec:
                msec = cap.get(cv2.CAP_PROP_POS_MSEC)
                if msec is None or not np.isfinite(msec) or msec < last_msec:
                    if index > 0:
                        use_msec = False
                        log.debug("Container timestamps unusable; using frame index.")
                else:
                    last_msec = msec
                    if msec > 0 or index == 0:
                        timestamp = msec / 1000.0

            ok, frame = cap.read()
            if not ok or frame is None:
                consecutive_failures += 1
                # A single failed read near the end of a slightly damaged file
                # is normal; several in a row means the stream is over.
                if consecutive_failures >= 3:
                    break
                index += 1
                continue
            consecutive_failures = 0

            yield timestamp, index, frame

            # Skip ahead without decoding whole frames.
            skipped = 0
            for _ in range(step - 1):
                if not cap.grab():
                    skipped = -1
                    break
                skipped += 1
            if skipped < 0:
                break
            index += step

    def read_frame_at(self, timestamp: float) -> Optional[np.ndarray]:
        """Random-access read, used only by debug tooling (it breaks the
        sequential read position, so never call it during the main pass)."""
        cv2 = self._cv2
        self.capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
        ok, frame = self.capture.read()
        return frame if ok else None
