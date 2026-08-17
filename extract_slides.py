#!/usr/bin/env python3
"""
extract_slides.py - pull one clean screenshot per presentation slide out of a
lecture recording.

    python extract_slides.py presentation.mp4
    python extract_slides.py presentation.mp4 --output slides --sample-rate 2 --threshold 0.90

Frames are sampled a couple of times per second, compared with SSIM, and a
screenshot is written only once a *new* picture has been visibly stable for a
moment.  See README.md for the full explanation.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

__version__ = "1.0.0"

# Dependencies are imported lazily so that a missing package produces a helpful
# message instead of a traceback.
try:
    import numpy as np
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "NumPy is not installed.\n"
        "    python -m venv .venv\n"
        "    .venv\\Scripts\\activate      (Windows)\n"
        "    pip install -r requirements.txt\n"
    )
    raise SystemExit(1)

try:
    import cv2
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "OpenCV is not installed.\n"
        "Install the dependencies first:\n"
        "    pip install -r requirements.txt\n"
        "(the package is called 'opencv-python')\n"
    )
    raise SystemExit(1)

from config import Config, ConfigError, EXAMPLE_CONFIG
from detector import SlideDetector, SlideEvent
from utils import (
    OutputError,
    ProgressDisplay,
    SlideExtractorError,
    VideoError,
    ensure_directory,
    format_duration,
    format_timestamp,
    parse_crop,
    parse_time,
    save_image,
    setup_logging,
)
from video import VideoSource

log = None  # set up in main()


# --------------------------------------------------------------------- output
@dataclass
class SavedSlide:
    number: int
    filename: str
    appeared_at: float
    captured_at: float
    similarity: float
    path: Path

    def to_json(self) -> dict:
        return {
            "slide": self.number,
            "timestamp": format_timestamp(self.appeared_at),
            "filename": self.filename,
            "timestamp_seconds": round(self.appeared_at, 3),
            "captured_at": format_timestamp(self.captured_at),
            "similarity_to_previous": round(self.similarity, 4),
        }


class SlideWriter:
    """Owns the output directory: screenshots, slides.json and slides.txt."""

    def __init__(self, config: Config, output_dir: Path, video_name: str):
        self.config = config
        self.dir = output_dir
        self.video_name = video_name
        self.slides: List[SavedSlide] = []
        self._last_frame_shape = None

    def _filename(self, number: int) -> str:
        return f"slide_{number:03d}.{self.config.extension}"

    def add(self, event: SlideEvent, detector: SlideDetector) -> Optional[SavedSlide]:
        frame = event.frame
        if self.config.crop and self.config.crop_output:
            frame = detector.crop_frame(frame)

        if event.kind == "replace" and self.slides:
            slide = self.slides[-1]
            slide.captured_at = event.captured_at
            slide.similarity = event.similarity
            self._write(frame, slide)
            log.info(
                "  slide %03d updated at %s (%s)",
                slide.number, format_timestamp(event.captured_at), event.reason,
            )
            return None

        number = len(self.slides) + 1
        slide = SavedSlide(
            number=number,
            filename=self._filename(number),
            appeared_at=event.appeared_at,
            captured_at=event.captured_at,
            similarity=event.similarity,
            path=self.dir / self._filename(number),
        )
        self._write(frame, slide)
        self.slides.append(slide)
        log.info(
            "  slide %03d  %s  (ssim vs previous %.3f)",
            slide.number, format_timestamp(slide.appeared_at), slide.similarity,
        )
        return slide

    def _write(self, frame, slide: SavedSlide) -> None:
        save_image(
            frame,
            slide.path,
            jpeg_quality=self.config.jpeg_quality,
            metadata={
                "Software": f"extract_slides.py {__version__}",
                "Source": self.video_name,
                "SlideNumber": slide.number,
                "Timestamp": format_timestamp(slide.appeared_at),
            },
        )
        self._last_frame_shape = frame.shape

    # --------------------------------------------------------------- metadata
    def write_metadata(self, video_info, config: Config, partial: bool = False) -> None:
        if not self.slides:
            return
        json_path = self.dir / "slides.json"
        payload = [slide.to_json() for slide in self.slides]
        try:
            json_path.write_text(
                json.dumps(payload, indent=4, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            raise OutputError(f"Could not write {json_path}: {exc}")

        txt_path = self.dir / "slides.txt"
        width = max((len(s.filename) for s in self.slides), default=12)
        lines = [
            f"Slides extracted from: {self.video_name}",
            f"Video duration:        {format_duration(video_info.duration)}",
            f"Resolution:            {video_info.width}x{video_info.height}",
            f"Slides found:          {len(self.slides)}"
            + ("   (INCOMPLETE - interrupted)" if partial else ""),
            "",
            f"{'#':>4}  {'TIMESTAMP':<13}  {'FILE':<{width}}",
            f"{'-' * 4}  {'-' * 13}  {'-' * width}",
        ]
        for slide in self.slides:
            lines.append(
                f"{slide.number:>4}  {format_timestamp(slide.appeared_at):<13}  "
                f"{slide.filename:<{width}}"
            )
        lines += [
            "",
            "Settings used:",
            f"  sample_rate         {config.sample_rate}",
            f"  change_threshold    {config.change_threshold}",
            f"  stability_threshold {config.stability_threshold}",
            f"  stability_duration  {config.stability_duration}",
            f"  min_slide_duration  {config.min_slide_duration}",
            f"  duplicate_threshold {config.duplicate_threshold}",
            f"  crop                {config.crop or 'none'}",
            "",
        ]
        try:
            txt_path.write_text("\n".join(lines), encoding="utf-8")
        except OSError as exc:
            raise OutputError(f"Could not write {txt_path}: {exc}")


class DebugRecorder:
    """Optional: writes before/after/diff images and a per-sample CSV."""

    def __init__(self, directory: Path, detector: SlideDetector):
        self.dir = ensure_directory(directory, purpose="debug")
        self.detector = detector
        self.count = 0
        self.csv_path = self.dir / "similarity.csv"
        self._csv_file = self.csv_path.open("w", newline="", encoding="utf-8")
        self._csv = csv.writer(self._csv_file)
        self._csv.writerow(
            ["timestamp", "seconds", "frame", "state", "ssim_vs_reference",
             "ssim_vs_previous", "changed_fraction", "stability_threshold", "note"]
        )

    def record_sample(self, record) -> None:
        if record is None:
            return
        self._csv.writerow([
            format_timestamp(record.timestamp),
            f"{record.timestamp:.3f}",
            record.frame_index,
            record.state,
            f"{record.similarity_reference:.4f}",
            f"{record.similarity_previous:.4f}",
            f"{record.changed_fraction:.4f}",
            f"{record.stability_threshold:.4f}",
            record.note,
        ])

    def record_transition(self, before, after, event: SlideEvent, jpeg_quality: int) -> None:
        self.count += 1
        stem = f"transition_{self.count:03d}"
        if before is not None:
            save_image(before, self.dir / f"{stem}_before.png", jpeg_quality)
        save_image(after, self.dir / f"{stem}_after.png", jpeg_quality)
        if before is not None:
            heat = self.detector.similarity_visual(before, after)
            blended = cv2.addWeighted(self.detector.crop_frame(after), 0.45, heat, 0.55, 0)
            save_image(blended, self.dir / f"{stem}_diff.png", jpeg_quality)
        note = (
            f"{stem}\n"
            f"  slide change at : {format_timestamp(event.appeared_at)}\n"
            f"  frame captured  : {format_timestamp(event.captured_at)}\n"
            f"  kind            : {event.kind}\n"
            f"  similarity      : {event.similarity:.4f}\n"
            f"  reason          : {event.reason}\n"
        )
        with (self.dir / "transitions.txt").open("a", encoding="utf-8") as handle:
            handle.write(note + "\n")

    def close(self) -> None:
        try:
            self._csv_file.close()
        except Exception:
            pass


# ------------------------------------------------------------------ extraction
@dataclass
class Result:
    slides: int = 0
    samples: int = 0
    duplicates: int = 0
    builds: int = 0
    aborted: int = 0
    interrupted: bool = False
    output_dir: Path = field(default_factory=Path)


def extract(video_path: Path, config: Config) -> Result:
    output_dir = ensure_directory(Path(config.output_dir), purpose="output")

    with VideoSource(video_path) as source:
        info = source.info
        print("Processing video...")
        print(info.describe())
        print()

        if config.end and info.duration and config.end > info.duration + 1:
            log.warning(
                "--end %s is beyond the video duration %s; using the whole video.",
                format_duration(config.end), format_duration(info.duration),
            )
        if config.start and info.duration and config.start >= info.duration:
            raise VideoError(
                f"--start {format_duration(config.start)} is at or past the end of the "
                f"video ({format_duration(info.duration)})"
            )

        detector = SlideDetector(config, (info.width, info.height))
        writer = SlideWriter(config, output_dir, info.path.name)
        debug = DebugRecorder(Path(config.debug_dir), detector) if config.debug else None

        if config.crop:
            log.info(
                "Detection region: x=%d y=%d w=%d h=%d%s",
                *detector.crop_region,
                " (also applied to saved screenshots)" if config.crop_output else "",
            )
        log.info(
            "Comparing at %dx%d, sampling %.2f fps, change threshold %.2f\n",
            detector.work_size[0], detector.work_size[1],
            config.sample_rate, config.change_threshold,
        )

        span_end = min(config.end or info.duration, info.duration) if info.duration else config.end
        span_start = config.start or 0.0
        total = max((span_end or 0.0) - span_start, 0.0)
        progress = ProgressDisplay(total_seconds=total, enabled=not config.quiet)

        previous_saved_frame = None
        result = Result(output_dir=output_dir)
        timestamp = span_start

        try:
            for timestamp, frame_index, frame in source.iter_samples(
                config.sample_rate, start=config.start, end=config.end
            ):
                events = detector.process(timestamp, frame_index, frame)
                if debug:
                    debug.record_sample(detector.last_record)

                for event in events:
                    progress.finish()
                    writer.add(event, detector)
                    if debug:
                        debug.record_transition(
                            previous_saved_frame, event.frame, event, config.jpeg_quality
                        )
                    previous_saved_frame = event.frame

                progress.update(timestamp - span_start, len(writer.slides))

            for event in detector.finalize():
                progress.finish()
                writer.add(event, detector)
                if debug:
                    debug.record_transition(
                        previous_saved_frame, event.frame, event, config.jpeg_quality
                    )

        except KeyboardInterrupt:
            progress.finish()
            result.interrupted = True
            print("\nInterrupted. Writing metadata for the slides found so far...")
        finally:
            progress.finish()
            if debug:
                debug.close()

        writer.write_metadata(info, config, partial=result.interrupted)

        result.slides = len(writer.slides)
        result.samples = detector.stats.samples
        result.duplicates = detector.stats.duplicates_skipped
        result.builds = detector.stats.builds_merged
        result.aborted = detector.stats.transitions_aborted
        return result


# ------------------------------------------------------------------------ CLI
def build_parser() -> argparse.ArgumentParser:
    defaults = Config()
    parser = argparse.ArgumentParser(
        prog="extract_slides.py",
        description="Extract one screenshot per presentation slide from a video.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python extract_slides.py lecture.mp4
  python extract_slides.py lecture.mp4 --output slides --sample-rate 2 --threshold 0.90
  python extract_slides.py lecture.mp4 --crop 0,0,1920,900     # ignore a webcam strip
  python extract_slides.py lecture.mp4 --debug                 # explain every decision

crop format:
  --crop X,Y,WIDTH,HEIGHT  in pixels of the original video, origin top-left.
  Only that region is used to decide whether the slide changed; the saved
  screenshot still shows the whole frame unless --crop-output is given.
  Example: a 1920x1080 recording with a webcam in the bottom-right corner
  and the slide filling the top 900 pixels -> --crop 0,0,1920,900
""",
    )
    parser.add_argument("video", nargs="?", help="path to the video file")
    parser.add_argument(
        "-o", "--output", default=defaults.output_dir,
        help=f"directory for the screenshots (default: {defaults.output_dir})",
    )
    parser.add_argument(
        "-r", "--sample-rate", type=float, default=None, metavar="FPS",
        help=f"frames analysed per second (default: {defaults.sample_rate})",
    )
    parser.add_argument(
        "-t", "--threshold", type=float, default=None, metavar="0..1",
        help=(
            "similarity below which a slide change is suspected "
            f"(default: {defaults.change_threshold}; lower = fewer slides)"
        ),
    )
    parser.add_argument(
        "--min-slide-duration", type=float, default=None, metavar="SECONDS",
        help=(
            "a new picture appearing sooner than this after the previous slide is "
            f"treated as an animation build and replaces it (default: {defaults.min_slide_duration})"
        ),
    )
    parser.add_argument(
        "-f", "--format", choices=("png", "jpg"), default=None,
        help=f"screenshot format (default: {defaults.output_format})",
    )
    parser.add_argument(
        "--stability-threshold", type=float, default=None, metavar="0..1",
        help=(
            "similarity between consecutive frames that counts as 'not moving' "
            f"(default: {defaults.stability_threshold})"
        ),
    )
    parser.add_argument(
        "--stability-duration", type=float, default=None, metavar="SECONDS",
        help=(
            "how long the picture must hold still before it is captured "
            f"(default: {defaults.stability_duration})"
        ),
    )
    parser.add_argument(
        "--duplicate-threshold", type=float, default=None, metavar="0..1",
        help=(
            "similarity above which a candidate is discarded as a duplicate "
            f"(default: {defaults.duplicate_threshold})"
        ),
    )
    parser.add_argument(
        "--crop", default=None, metavar="X,Y,W,H",
        help="only compare this region (see crop format below)",
    )
    parser.add_argument(
        "--crop-output", action="store_true",
        help="also crop the saved screenshots, not just the comparison",
    )
    parser.add_argument(
        "--no-auto-mask", action="store_true",
        help="disable automatic down-weighting of permanently moving regions",
    )
    parser.add_argument(
        "--work-width", type=int, default=None, metavar="PX",
        help=f"width frames are downscaled to for comparison (default: {defaults.work_width})",
    )
    parser.add_argument("--start", default=None, metavar="TIME",
                        help="skip everything before this point (SS, MM:SS or HH:MM:SS)")
    parser.add_argument("--end", default=None, metavar="TIME",
                        help="stop at this point (SS, MM:SS or HH:MM:SS)")
    parser.add_argument(
        "--jpeg-quality", type=int, default=None, metavar="1..100",
        help=f"JPEG quality when --format jpg (default: {defaults.jpeg_quality})",
    )
    parser.add_argument("--config", default=None, metavar="FILE",
                        help="JSON file with the settings above")
    parser.add_argument("--write-config", default=None, metavar="FILE",
                        help="write an example config file and exit")
    parser.add_argument("--debug", action="store_true",
                        help="verbose logging plus before/after/diff images in ./debug")
    parser.add_argument("--debug-dir", default=None, metavar="DIR",
                        help=f"where debug output goes (default: {defaults.debug_dir})")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="only warnings and errors, no progress line")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def config_from_args(args: argparse.Namespace) -> Config:
    """Defaults < config file < command line flags."""
    config = Config.from_file(args.config) if args.config else Config()

    if args.sample_rate is not None:
        config.sample_rate = args.sample_rate
    if args.threshold is not None:
        config.change_threshold = args.threshold
    if args.stability_threshold is not None:
        config.stability_threshold = args.stability_threshold
    if args.stability_duration is not None:
        config.stability_duration = args.stability_duration
    if args.min_slide_duration is not None:
        config.min_slide_duration = args.min_slide_duration
    if args.duplicate_threshold is not None:
        config.duplicate_threshold = args.duplicate_threshold
    if args.work_width is not None:
        config.work_width = args.work_width
    if args.format is not None:
        config.output_format = args.format
    if args.jpeg_quality is not None:
        config.jpeg_quality = args.jpeg_quality
    if args.output is not None:
        config.output_dir = args.output
    if args.debug_dir is not None:
        config.debug_dir = args.debug_dir
    if args.crop is not None:
        config.crop = parse_crop(args.crop)
    if args.crop_output:
        config.crop_output = True
    if args.no_auto_mask:
        config.auto_mask = False
    if args.start is not None:
        config.start = parse_time(args.start)
    if args.end is not None:
        config.end = parse_time(args.end)
    if args.debug:
        config.debug = True
    if args.quiet:
        config.quiet = True

    # A raised change threshold that ends up above the stability threshold is
    # the single most common mistake; nudge it instead of failing.
    if config.stability_threshold < config.change_threshold:
        config.stability_threshold = min(0.999, (1.0 + config.change_threshold) / 2)
    if config.duplicate_threshold < config.change_threshold:
        config.duplicate_threshold = config.change_threshold

    config.validate()
    return config


def main(argv: Optional[List[str]] = None) -> int:
    global log
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if args.write_config:
        target = Path(args.write_config)
        try:
            target.write_text(json.dumps(EXAMPLE_CONFIG, indent=4) + "\n", encoding="utf-8")
        except OSError as exc:
            sys.stderr.write(f"Error: could not write {target}: {exc}\n")
            return 1
        print(f"Example configuration written to {target}")
        print("Edit it, then run:  python extract_slides.py video.mp4 --config " + str(target))
        return 0

    if not args.video:
        parser.error("the following arguments are required: video")

    log = setup_logging(debug=args.debug, quiet=args.quiet)

    try:
        config = config_from_args(args)
    except (ConfigError, ValueError) as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2

    try:
        result = extract(Path(args.video), config)
    except SlideExtractorError as exc:
        sys.stderr.write(f"\nError: {exc}\n")
        return 1
    except ValueError as exc:
        sys.stderr.write(f"\nError: {exc}\n")
        return 2
    except MemoryError:
        sys.stderr.write(
            "\nError: out of memory. Try a smaller --work-width or a lower --sample-rate.\n"
        )
        return 1

    print()
    print("Processing complete." if not result.interrupted else "Processing interrupted.")
    print()
    print(f"Slides detected:  {result.slides}")
    print(f"Frames analysed:  {result.samples}")
    if result.builds:
        print(f"Builds merged:    {result.builds} (animation steps folded into their slide)")
    if result.duplicates:
        print(f"Duplicates skipped: {result.duplicates}")
    print(f"Output directory: {result.output_dir}")
    if result.slides:
        print(f"Metadata:         {result.output_dir / 'slides.json'}, "
              f"{result.output_dir / 'slides.txt'}")
    if config.debug:
        print(f"Debug output:     {Path(config.debug_dir)}")

    if result.slides == 0:
        sys.stderr.write(
            "\nNo slides were detected. The video may be empty, or the whole clip may "
            "be a single unchanging picture.\n"
            "Try:  --threshold 0.95 --stability-duration 0.5 --min-slide-duration 1\n"
        )
        return 3
    if result.slides == 1 and result.samples > 20:
        print(
            "\nOnly one slide was found. If the video really does change, raise the "
            "threshold (e.g. --threshold 0.95) or add --debug to see the similarity "
            "values in debug/similarity.csv."
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.stderr.write("\nAborted.\n")
        raise SystemExit(130)
