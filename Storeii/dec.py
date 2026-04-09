from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.append(str(Path(__file__).resolve().parent.parent))

from Storeii.codec import DEFAULT_PIXEL_SIZE, decode_video_to_file  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Decode a Storeii video back into the original file.")
    parser.add_argument("input_video", help="Path to the Storeii video.")
    parser.add_argument(
        "output_dir",
        nargs="?",
        default=".",
        help="Directory where the decoded file should be written.",
    )
    parser.add_argument(
        "--output-name",
        help="Override the decoded filename. By default the embedded original filename is used.",
    )
    parser.add_argument(
        "--pix",
        type=int,
        default=DEFAULT_PIXEL_SIZE,
        help="Pixel block size that was used when encoding the video.",
    )
    args = parser.parse_args()

    result = decode_video_to_file(
        args.input_video,
        args.output_dir,
        output_filename=args.output_name,
        pix=args.pix,
    )
    format_label = "legacy" if result.legacy_format else "v1"
    print(
        f"Decoded {args.input_video} into {result.output_path} "
        f"({result.recovered_bytes} bytes, format={format_label})."
    )


if __name__ == "__main__":
    main()
