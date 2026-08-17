"""
Configuration for the video slide extractor.

Every tunable parameter lives here so that there is exactly one place to look
when the detector needs adjusting.  Values can come from three places, in order
of increasing priority:

    1. The defaults in ``Config`` below.
    2. A JSON config file passed with ``--config myconfig.json``.
    3. Explicit command line flags.

Parameter reference
-------------------

sample_rate (float, frames per second)
    How many frames per second are analysed.  The video is *not* fully
    decoded: frames in between are skipped with ``cap.grab()``.  1-2 is
    normally right.  Raising it finds short-lived slides and makes timestamps
    more precise, at the cost of speed.

change_threshold (float, 0..1)
    Similarity (SSIM) below which the current frame is considered "different
    from the slide currently on screen", i.e. a possible slide change.
    0.90 is a good starting point.  Lower it (0.80) if the detector is too
    trigger-happy, raise it (0.95) if it misses slides that look similar to
    their predecessor.

stability_threshold (float, 0..1)
    Similarity between two *consecutive* sampled frames above which the
    picture is considered "not moving any more".  Used to wait out fades and
    build animations.  Should be higher than ``change_threshold``.

stability_duration (float, seconds)
    How long the picture must stay stable (per ``stability_threshold``) before
    the new slide is captured.  Increase it for presentations with long fade
    or wipe transitions; decrease it for fast slide flipping.

min_slide_duration (float, seconds)
    Minimum time a slide must survive to be treated as its own slide.  If a
    new stable picture appears sooner than this after the previous capture,
    it is assumed to be a later stage of the same slide (a PowerPoint "build"
    animation) and it *replaces* the previous screenshot instead of adding a
    new one.  That keeps the most complete version of each slide.

duplicate_threshold (float, 0..1)
    Similarity above which a candidate screenshot is considered identical to
    the previously saved slide and is dropped.  Guards against fade-out /
    fade-in to the same slide.

auto_mask (bool)
    Learn which parts of the frame move constantly (a lecturer webcam, a
    clock, a player progress bar) and give them less weight when comparing
    frames.  Without it, any permanently moving region keeps the similarity
    score low forever and the detector can never settle.  Turn it off with
    ``--no-auto-mask`` if the slide area itself is animated most of the time.

mask_sensitivity (float, grey levels)
    How much average per-pixel movement is needed before a region is
    considered "busy" and down-weighted.  Lower = more aggressive masking.

work_width (int, pixels)
    Width the frames are downscaled to before comparison.  Comparison happens
    on a small normalised grayscale image, which is what makes the detector
    immune to video compression noise.  Screenshots are always saved at the
    original resolution.

output_format ("png" | "jpg")
    Screenshot file format.  PNG is the default because slides are mostly
    text and JPEG artefacts hurt readability.

jpeg_quality (int, 1..100)
    Only used when ``output_format`` is "jpg".

crop (tuple[int, int, int, int] | None)
    ``(x, y, width, height)`` region of the frame used *for detection only*.
    Use it to exclude a lecturer webcam or a moving progress bar.  Saved
    screenshots still contain the full frame unless ``crop_output`` is set.

crop_output (bool)
    When true, the crop region is also applied to the saved screenshots.

start / end (float seconds | None)
    Optional trim of the analysed part of the video.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Optional, Tuple


@dataclass
class Config:
    # --- sampling -------------------------------------------------------
    sample_rate: float = 2.0
    start: Optional[float] = None
    end: Optional[float] = None

    # --- detection ------------------------------------------------------
    change_threshold: float = 0.90
    stability_threshold: float = 0.985
    stability_duration: float = 1.0
    min_slide_duration: float = 2.0
    duplicate_threshold: float = 0.98
    work_width: int = 320
    auto_mask: bool = True
    mask_sensitivity: float = 8.0

    # --- region of interest ---------------------------------------------
    crop: Optional[Tuple[int, int, int, int]] = None
    crop_output: bool = False

    # --- output ---------------------------------------------------------
    output_dir: str = "output"
    output_format: str = "png"
    jpeg_quality: int = 95

    # --- diagnostics ------------------------------------------------------
    debug: bool = False
    debug_dir: str = "debug"
    quiet: bool = False

    # ---------------------------------------------------------------- load
    @classmethod
    def from_file(cls, path: str | Path) -> "Config":
        """Build a Config from a JSON file, ignoring unknown keys loudly."""
        path = Path(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ConfigError(f"Config file not found: {path}")
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Config file {path} is not valid JSON: {exc}")
        if not isinstance(raw, dict):
            raise ConfigError(f"Config file {path} must contain a JSON object")

        known = {f.name for f in fields(cls)}
        unknown = set(raw) - known
        if unknown:
            raise ConfigError(
                f"Unknown keys in {path}: {', '.join(sorted(unknown))}. "
                f"Valid keys: {', '.join(sorted(known))}"
            )
        cfg = cls(**raw)
        if cfg.crop is not None:
            cfg.crop = tuple(int(v) for v in cfg.crop)  # type: ignore[assignment]
        cfg.validate()
        return cfg

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # ------------------------------------------------------------ validate
    def validate(self) -> None:
        """Reject nonsensical combinations early with a readable message."""
        if self.sample_rate <= 0:
            raise ConfigError("sample_rate must be greater than 0")
        if self.sample_rate > 30:
            raise ConfigError("sample_rate above 30 fps is not useful; lower it")
        for name in ("change_threshold", "stability_threshold", "duplicate_threshold"):
            value = getattr(self, name)
            if not 0.0 < value <= 1.0:
                raise ConfigError(f"{name} must be in the range (0, 1], got {value}")
        if self.stability_threshold < self.change_threshold:
            raise ConfigError(
                "stability_threshold must be >= change_threshold "
                f"(got {self.stability_threshold} < {self.change_threshold})"
            )
        if self.stability_duration < 0:
            raise ConfigError("stability_duration must be >= 0")
        if self.min_slide_duration < 0:
            raise ConfigError("min_slide_duration must be >= 0")
        if self.work_width < 64:
            raise ConfigError("work_width must be at least 64 pixels")
        if self.mask_sensitivity <= 0:
            raise ConfigError("mask_sensitivity must be greater than 0")
        if self.output_format.lower() not in ("png", "jpg", "jpeg"):
            raise ConfigError("output_format must be 'png' or 'jpg'")
        if not 1 <= self.jpeg_quality <= 100:
            raise ConfigError("jpeg_quality must be between 1 and 100")
        if self.crop is not None:
            if len(self.crop) != 4:
                raise ConfigError("crop must have exactly 4 values: x,y,width,height")
            x, y, w, h = self.crop
            if x < 0 or y < 0:
                raise ConfigError("crop x and y must be >= 0")
            if w <= 0 or h <= 0:
                raise ConfigError("crop width and height must be > 0")
        if self.start is not None and self.start < 0:
            raise ConfigError("start must be >= 0")
        if self.start is not None and self.end is not None and self.end <= self.start:
            raise ConfigError("end must be greater than start")

    @property
    def extension(self) -> str:
        return "jpg" if self.output_format.lower() in ("jpg", "jpeg") else "png"


class ConfigError(ValueError):
    """Raised for invalid configuration values."""


#: Written by ``--write-config``; also serves as documentation of the format.
EXAMPLE_CONFIG = {
    "sample_rate": 2.0,
    "change_threshold": 0.90,
    "stability_threshold": 0.985,
    "stability_duration": 1.0,
    "min_slide_duration": 2.0,
    "duplicate_threshold": 0.98,
    "work_width": 320,
    "output_format": "png",
    "output_dir": "output",
}
