"""Slide change detection.

The detector never looks at raw pixels directly.  Every frame is first reduced
to a small, blurred, normalised grayscale image; two of those are then compared
with SSIM (structural similarity).  SSIM is used instead of a plain pixel diff
because it reacts to *structure* (text, shapes, layout) and largely ignores the
block noise, brightness ripple and chroma drift that video compression adds to
an otherwise unchanged slide.

On top of the similarity measure sits a two state machine:

    STABLE      the slide currently on screen still matches the reference
    TRANSITION  something changed; wait until the picture settles again

A screenshot is only taken once the picture has been quiet for
``stability_duration`` seconds, which is what keeps half-faded transition
frames out of the output.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import cv2
import numpy as np

from config import Config

log = logging.getLogger("slides")

# SSIM stabilising constants for 8 bit data (Wang et al. 2004).
_C1 = (0.01 * 255) ** 2
_C2 = (0.03 * 255) ** 2
_GAUSS_KSIZE = (11, 11)
_GAUSS_SIGMA = 1.5

# Local variance (in grey levels squared) at which a region starts to count as
# "content" rather than "empty background". See ``ssim_parts``.
_INFO_SCALE = 25.0


# --------------------------------------------------------------------- SSIM
def ssim_parts(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return the per pixel SSIM map and a per pixel information weight.

    The weight is ``log(1 + local_variance / scale)`` of the busier of the two
    images, which is the information-content weighting of Wang & Li (2011).
    It matters a great deal here: a slide is mostly empty background, so a
    plain average over the SSIM map stays around 0.95 even when every line of
    text has been replaced.  Weighting by local structure makes the score
    depend on the parts of the slide that actually carry information, and a
    real slide change then drops it to 0.3-0.6.
    """
    blur = lambda img: cv2.GaussianBlur(img, _GAUSS_KSIZE, _GAUSS_SIGMA)

    mu_a = blur(a)
    mu_b = blur(b)
    mu_a2, mu_b2, mu_ab = mu_a * mu_a, mu_b * mu_b, mu_a * mu_b

    sigma_a2 = blur(a * a) - mu_a2
    sigma_b2 = blur(b * b) - mu_b2
    sigma_ab = blur(a * b) - mu_ab

    numerator = (2 * mu_ab + _C1) * (2 * sigma_ab + _C2)
    denominator = (mu_a2 + mu_b2 + _C1) * (sigma_a2 + sigma_b2 + _C2)
    smap = np.clip(numerator / denominator, 0.0, 1.0)

    variance = np.maximum(np.maximum(sigma_a2, sigma_b2), 0.0)
    info = np.log1p(variance / _INFO_SCALE)
    return smap, info


def ssim_map(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Per pixel structural similarity between two float32 grayscale images."""
    return ssim_parts(a, b)[0]


def ssim(
    a: np.ndarray,
    b: np.ndarray,
    weights: Optional[np.ndarray] = None,
    content_weighted: bool = True,
) -> float:
    """Scalar similarity in ``[0, 1]``.

    ``weights`` optionally down-weights image regions that are known to move
    on their own (see :class:`MotionMask`); it is combined with the structure
    weighting described in :func:`ssim_parts`.
    """
    smap, info = ssim_parts(a, b)

    combined: Optional[np.ndarray] = None
    if content_weighted and float(info.max()) > 0.05:
        combined = info
    if weights is not None:
        combined = weights if combined is None else combined * weights

    if combined is not None:
        total = float(combined.sum())
        if total > 1e-6:
            return float((smap * combined).sum() / total)
    return float(smap.mean())


def diff_ratio(a: np.ndarray, b: np.ndarray, tolerance: float = 25.0) -> float:
    """Fraction of pixels that differ by more than ``tolerance`` grey levels.

    Not used for decisions; it is a second opinion that makes the debug log
    much easier to read than SSIM alone.
    """
    return float((np.abs(a - b) > tolerance).mean())


# ---------------------------------------------------------------- motion mask
class MotionMask:
    """Learns which parts of the frame move all the time.

    A lecturer webcam, a running clock or a player progress bar changes on
    every single frame.  Left alone it drags the similarity score down forever
    and the detector can never settle.  This class keeps a running average of
    the per pixel change between consecutive *similar* frames and turns it into
    a weight map: quiet regions weigh 1, permanently busy regions weigh close
    to 0.  Slide content therefore decides slide changes.
    """

    #: A frame pair where more than this fraction of pixels changed is a real
    #: picture change, not local motion, and must not be learned as "busy".
    LEARN_MAX_CHANGE = 0.30

    def __init__(self, sensitivity: float = 12.0, alpha: float = 0.10):
        self.sensitivity = float(sensitivity)
        self.alpha = float(alpha)
        self.volatility: Optional[np.ndarray] = None
        self.samples = 0

    def update(self, previous: np.ndarray, current: np.ndarray) -> None:
        delta = np.abs(current - previous)
        if self.volatility is None or self.volatility.shape != delta.shape:
            self.volatility = delta.copy()
        else:
            self.volatility += self.alpha * (delta - self.volatility)
        self.samples += 1

    def weights(self) -> Optional[np.ndarray]:
        # Below a handful of samples the estimate is noise, not information.
        if self.volatility is None or self.samples < 4:
            return None
        ratio = self.volatility / max(self.sensitivity, 1e-6)
        weight = 1.0 / (1.0 + ratio * ratio)
        # If nearly everything is busy (full screen video), weighting adds
        # nothing; fall back to an unweighted comparison.
        if float(weight.mean()) < 0.15:
            return None
        return weight.astype(np.float32)

    def busy_fraction(self) -> float:
        weights = self.weights()
        if weights is None:
            return 0.0
        return float((weights < 0.5).mean())


# -------------------------------------------------------------------- noise
class NoiseFloor:
    """Running estimate of frame-to-frame similarity while nothing changes.

    How similar two consecutive frames of an *unchanging* slide are depends
    entirely on the recording: a clean screen capture scores 0.999, a
    re-encoded webcast with grain scores 0.94.  A single hard-coded stability
    threshold therefore either never triggers (nothing is ever captured) or
    triggers halfway through a fade.  This class watches the quiet stretches
    and reports the level below which movement is real rather than noise.
    """

    #: Minimum gap kept below the quiet average, so the estimate is usable
    #: after two or three samples instead of after a long warm-up.
    MIN_MARGIN = 0.02

    def __init__(self, alpha: float = 0.1, sigmas: float = 3.0, min_samples: int = 3):
        self.alpha = alpha
        self.sigmas = sigmas
        self.min_samples = min_samples
        self.mean: Optional[float] = None
        self.variance = 0.0
        self.count = 0

    def update(self, similarity: float) -> None:
        self.count += 1
        if self.mean is None:
            self.mean = similarity
            return
        # Behave like a plain running average until enough samples exist for
        # the exponential average to mean anything.
        rate = max(self.alpha, 1.0 / self.count)
        delta = similarity - self.mean
        self.mean += rate * delta
        self.variance += rate * (delta * delta - self.variance)

    def floor(self) -> Optional[float]:
        if self.mean is None or self.count < self.min_samples:
            return None
        spread = self.sigmas * float(np.sqrt(max(self.variance, 0.0)))
        return float(self.mean - max(spread, self.MIN_MARGIN))


# -------------------------------------------------------------------- events
@dataclass
class SlideEvent:
    """A slide that should be written to disk."""

    kind: str                  # "new" or "replace"
    appeared_at: float         # when the slide first became visible
    captured_at: float         # timestamp of the frame actually saved
    change_detected_at: float  # when the transition started
    frame: np.ndarray          # full resolution BGR frame
    similarity: float          # similarity against the previous saved slide
    reason: str = ""


@dataclass
class SampleRecord:
    """One analysed frame; used for the debug CSV."""

    timestamp: float
    frame_index: int
    state: str
    similarity_reference: float
    similarity_previous: float
    changed_fraction: float
    stability_threshold: float = 0.0
    note: str = ""


@dataclass
class _Candidate:
    small: np.ndarray
    frame: np.ndarray
    appeared_at: float
    captured_at: float
    change_detected_at: float
    stable_since: float


@dataclass
class DetectorStats:
    samples: int = 0
    transitions_started: int = 0
    transitions_aborted: int = 0
    duplicates_skipped: int = 0
    builds_merged: int = 0
    events: List[SlideEvent] = field(default_factory=list)


STATE_STABLE = "stable"
STATE_TRANSITION = "transition"


class SlideDetector:
    """Feed it sampled frames in order; it yields slides to save."""

    def __init__(self, config: Config, frame_size: Tuple[int, int]):
        self.config = config
        self.frame_width, self.frame_height = frame_size
        self.state = STATE_STABLE
        self.reference: Optional[np.ndarray] = None       # last saved slide (small)
        self.previous_small: Optional[np.ndarray] = None  # previous sampled frame
        self.candidate: Optional[_Candidate] = None
        self.last_commit_appeared: Optional[float] = None
        self.mask = MotionMask(sensitivity=config.mask_sensitivity) if config.auto_mask else None
        self.noise = NoiseFloor()
        self.stats = DetectorStats()
        self.last_record: Optional[SampleRecord] = None
        self._crop = self._resolve_crop()
        self._work_size = self._resolve_work_size()
        log.debug(
            "Detector working size: %sx%s, crop=%s, auto_mask=%s",
            self._work_size[0], self._work_size[1], self._crop, config.auto_mask,
        )

    # ------------------------------------------------------------ preparation
    def _resolve_crop(self) -> Optional[Tuple[int, int, int, int]]:
        from utils import clamp_crop

        if not self.config.crop:
            return None
        clamped = clamp_crop(self.config.crop, self.frame_width, self.frame_height)
        if clamped is None:
            raise ValueError(
                f"Crop {self.config.crop} lies completely outside the "
                f"{self.frame_width}x{self.frame_height} frame"
            )
        if clamped != tuple(self.config.crop):
            log.warning(
                "Crop %s clipped to %s to fit the %dx%d frame.",
                tuple(self.config.crop), clamped, self.frame_width, self.frame_height,
            )
        return clamped

    def _resolve_work_size(self) -> Tuple[int, int]:
        if self._crop:
            _, _, width, height = self._crop
        else:
            width, height = self.frame_width, self.frame_height
        target_width = min(self.config.work_width, width)
        scale = target_width / float(width)
        target_height = max(int(round(height * scale)), 32)
        return target_width, target_height

    def crop_frame(self, frame: np.ndarray) -> np.ndarray:
        if not self._crop:
            return frame
        x, y, width, height = self._crop
        return frame[y:y + height, x:x + width]

    def preprocess(self, frame: np.ndarray) -> np.ndarray:
        """Full frame -> small, blurred, normalised float32 grayscale."""
        region = self.crop_frame(frame)
        small = cv2.resize(region, self._work_size, interpolation=cv2.INTER_AREA)
        if small.ndim == 3:
            small = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        # Kills residual codec noise without touching real edges.
        small = cv2.GaussianBlur(small, (3, 3), 0)
        small = small.astype(np.float32)

        # Contrast normalisation makes the comparison immune to the slow
        # brightness/gamma drift of a re-encoded screen capture.  Skipped on
        # nearly uniform frames (a black fade), where it would only amplify
        # noise into fake structure.
        low, high = float(small.min()), float(small.max())
        if high - low > 24.0:
            small = (small - low) * (255.0 / (high - low))
        return small

    def stability_threshold(self) -> float:
        """Similarity above which the picture counts as "not moving".

        Starts at the configured value and relaxes towards the measured noise
        floor of this particular recording, but never as far down as the
        change threshold, otherwise a transition would look stable.
        """
        threshold = self.config.stability_threshold
        floor = self.noise.floor()
        if floor is not None:
            threshold = min(threshold, max(floor, self.config.change_threshold + 0.005))
        return threshold

    # ---------------------------------------------------------------- process
    def process(self, timestamp: float, frame_index: int, frame: np.ndarray) -> List[SlideEvent]:
        """Analyse one sampled frame. Returns 0 or 1 slide events."""
        cfg = self.config
        small = self.preprocess(frame)
        self.stats.samples += 1
        events: List[SlideEvent] = []
        note = ""
        change_started = False

        weights = self.mask.weights() if self.mask else None
        sim_previous = (
            ssim(small, self.previous_small, weights)
            if self.previous_small is not None else 1.0
        )
        changed = (
            diff_ratio(small, self.previous_small)
            if self.previous_small is not None else 0.0
        )

        sim_reference = (
            ssim(small, self.reference, weights) if self.reference is not None else 0.0
        )
        stability = self.stability_threshold()

        # --- bootstrap: the very first frame starts the first candidate ----
        if self.reference is None and self.candidate is None:
            self.candidate = _Candidate(
                small=small, frame=frame.copy(), appeared_at=timestamp,
                captured_at=timestamp, change_detected_at=timestamp,
                stable_since=timestamp,
            )
            self.state = STATE_TRANSITION
            note = "first frame"

        elif self.state == STATE_STABLE:
            if sim_reference < cfg.change_threshold:
                self.state = STATE_TRANSITION
                change_started = True
                self.stats.transitions_started += 1
                self.candidate = _Candidate(
                    small=small, frame=frame.copy(), appeared_at=timestamp,
                    captured_at=timestamp, change_detected_at=timestamp,
                    stable_since=timestamp,
                )
                note = f"change detected (ssim {sim_reference:.3f})"
                log.debug(
                    "[%.2fs] change detected, ssim vs reference %.4f", timestamp, sim_reference
                )

        else:  # STATE_TRANSITION
            assert self.candidate is not None
            sim_candidate = ssim(small, self.candidate.small, weights)
            if sim_candidate >= stability:
                # Picture is holding still. Keep the newest frame: it is the
                # most settled version of the slide.
                self.candidate.frame = frame.copy()
                self.candidate.captured_at = timestamp
                held = timestamp - self.candidate.stable_since
                if held >= cfg.stability_duration:
                    event = self._commit(timestamp, weights)
                    if event is not None:
                        events.append(event)
                        note = f"captured ({event.kind})"
                    else:
                        note = "duplicate, skipped"
                else:
                    note = f"stabilising {held:.1f}/{cfg.stability_duration:.1f}s"
            else:
                # Still moving: this frame becomes the new candidate and the
                # stability clock restarts.
                self.candidate.small = small
                self.candidate.frame = frame.copy()
                self.candidate.captured_at = timestamp
                self.candidate.stable_since = timestamp
                if sim_candidate < cfg.change_threshold:
                    # A genuinely different picture (still mid-transition), so
                    # this is the moment the slide "appears". Smaller wobbles
                    # only restart the stability clock, they do not move the
                    # reported appearance time.
                    self.candidate.appeared_at = timestamp
                note = f"still moving (ssim {sim_candidate:.3f})"

        # Learn from frame pairs that are broadly the same picture: a real
        # slide change (or a fade) must never be learned as "this region
        # always moves", or the detector would go blind to it.
        quiet_pair = (
            self.previous_small is not None
            and not change_started
            and changed < MotionMask.LEARN_MAX_CHANGE
        )
        if quiet_pair and self.mask is not None:
            self.mask.update(self.previous_small, small)
        if quiet_pair and sim_previous >= cfg.change_threshold:
            self.noise.update(sim_previous)

        self.previous_small = small
        self.last_record = SampleRecord(
            timestamp=timestamp,
            frame_index=frame_index,
            state=self.state,
            similarity_reference=sim_reference,
            similarity_previous=sim_previous,
            changed_fraction=changed,
            stability_threshold=stability,
            note=note,
        )
        return events

    # ----------------------------------------------------------------- commit
    def _commit(self, timestamp: float, weights) -> Optional[SlideEvent]:
        """Turn the settled candidate into a slide event (or drop it)."""
        cfg = self.config
        candidate = self.candidate
        assert candidate is not None

        similarity = (
            ssim(candidate.small, self.reference, weights)
            if self.reference is not None else 0.0
        )

        # Duplicate guard: fade out and fade back in to the same slide, or a
        # transient overlay (mouse, popup) that disappeared again.
        if self.reference is not None and similarity >= cfg.duplicate_threshold:
            self.stats.duplicates_skipped += 1
            self.stats.transitions_aborted += 1
            log.debug(
                "[%.2fs] candidate identical to last slide (ssim %.4f), skipped",
                timestamp, similarity,
            )
            self.reference = candidate.small
            self.candidate = None
            self.state = STATE_STABLE
            return None

        kind = "new"
        reason = f"ssim {similarity:.3f} < threshold {cfg.change_threshold}"
        if (
            self.last_commit_appeared is not None
            and candidate.appeared_at - self.last_commit_appeared < cfg.min_slide_duration
        ):
            # Too soon after the previous capture: this is almost certainly a
            # build animation on the same slide. Keep the newer, more complete
            # picture but do not add a slide.
            kind = "replace"
            self.stats.builds_merged += 1
            reason = (
                f"only {candidate.appeared_at - self.last_commit_appeared:.1f}s after the "
                f"previous slide (min_slide_duration {cfg.min_slide_duration}s): "
                "treated as an animation build"
            )
            log.debug("[%.2fs] %s", timestamp, reason)

        event = SlideEvent(
            kind=kind,
            appeared_at=candidate.appeared_at,
            captured_at=candidate.captured_at,
            change_detected_at=candidate.change_detected_at,
            frame=candidate.frame,
            similarity=similarity,
            reason=reason,
        )
        self.reference = candidate.small
        if kind == "new":
            self.last_commit_appeared = candidate.appeared_at
        self.candidate = None
        self.state = STATE_STABLE
        self.stats.events.append(event)
        return event

    def finalize(self) -> List[SlideEvent]:
        """Flush a candidate that was still stabilising when the video ended."""
        if self.state != STATE_TRANSITION or self.candidate is None:
            return []
        weights = self.mask.weights() if self.mask else None
        log.debug("End of video reached with a pending candidate; committing it.")
        event = self._commit(self.candidate.captured_at, weights)
        return [event] if event is not None else []

    # ------------------------------------------------------------------ debug
    @property
    def work_size(self) -> Tuple[int, int]:
        return self._work_size

    @property
    def crop_region(self) -> Optional[Tuple[int, int, int, int]]:
        return self._crop

    def similarity_visual(self, before: np.ndarray, after: np.ndarray) -> np.ndarray:
        """Colour heatmap of where two frames differ (debug output)."""
        smap = np.clip(ssim_map(self.preprocess(before), self.preprocess(after)), 0.0, 1.0)
        heat = ((1.0 - smap) * 255).astype(np.uint8)
        heat = cv2.applyColorMap(heat, cv2.COLORMAP_INFERNO)
        target = self.crop_frame(after)
        return cv2.resize(heat, (target.shape[1], target.shape[0]), interpolation=cv2.INTER_NEAREST)
