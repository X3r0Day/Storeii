from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.append(str(Path(__file__).resolve().parent.parent))

from Storeii.codec import (  # noqa: E402
    DEFAULT_FRAME_RATE,
    DEFAULT_HEIGHT,
    DEFAULT_PIXEL_SIZE,
    DEFAULT_WIDTH,
    StoreiiError,
    encode_file_to_video,
)


def _progress(stream):
    last = -1

    def show(done, total):
        nonlocal last
        pct = done * 100 // total if total else -1
        if pct == last and done != total:
            return
        last = pct
        stream.write(f"\r  frame {done}/{total} ({pct}%)" if total else f"\r  frame {done}")
        stream.flush()

    return show


def main() -> None:
    parser = argparse.ArgumentParser(description="Encode any file into a lossless video.")
    parser.add_argument("input_file", help="Path to the file you want to encode.")
    parser.add_argument("output_video", help="Path to the output video file, e.g. output.avi.")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH, help="Video width.")
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT, help="Video height.")
    parser.add_argument(
        "--frame-rate",
        type=int,
        default=DEFAULT_FRAME_RATE,
        help="Video frame rate.",
    )
    parser.add_argument(
        "--pix",
        type=int,
        default=DEFAULT_PIXEL_SIZE,
        help="Pixel block size used for each encoded bit.",
    )
    args = parser.parse_args()

    print(f"Encoding {args.input_file} ...", file=sys.stderr, flush=True)
    try:
        result = encode_file_to_video(
            args.input_file,
            args.output_video,
            width=args.width,
            height=args.height,
            frame_rate=args.frame_rate,
            pix=args.pix,
            progress=_progress(sys.stderr),
        )
    except StoreiiError as exc:
        sys.stderr.write("\r\x1b[K")
        raise SystemExit(f"encode failed: {exc}")

    sys.stderr.write("\r\x1b[K")
    print(
        f"Encoded {result.input_name} into {result.output_path} "
        f"({result.frame_count} frames, {result.video_bytes} bytes)."
    )


if __name__ == "__main__":
    main()
