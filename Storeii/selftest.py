from __future__ import annotations

import random
import sys
import tempfile
from pathlib import Path

import cv2

if __package__ in (None, ""):
    sys.path.append(str(Path(__file__).resolve().parent.parent))

from Storeii.codec import decode_video_to_file, encode_file_to_video


def _reencode(src: Path, dst: Path, factor: float) -> None:
    cap = cv2.VideoCapture(str(src))
    ok, frame = cap.read()
    if not ok:
        cap.release()
        raise RuntimeError(f"no frames in {src}")
    h, w = frame.shape[:2]
    size = (int(w * factor) // 2 * 2, int(h * factor) // 2 * 2)
    out = cv2.VideoWriter(str(dst), cv2.VideoWriter_fourcc(*"png "), 1, size, isColor=False)
    while ok:
        if frame.ndim == 3:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        out.write(cv2.resize(frame, size, interpolation=cv2.INTER_AREA))
        ok, frame = cap.read()
    out.release()
    cap.release()


def _repeat(src: Path, dst: Path, times: int) -> None:
    cap = cv2.VideoCapture(str(src))
    ok, frame = cap.read()
    if not ok:
        cap.release()
        raise RuntimeError(f"no frames in {src}")
    h, w = frame.shape[:2]
    out = cv2.VideoWriter(str(dst), cv2.VideoWriter_fourcc(*"png "), 1, (w, h), isColor=False)
    while ok:
        if frame.ndim == 3:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        for _ in range(times):
            out.write(frame)
        ok, frame = cap.read()
    out.release()
    cap.release()


def _check(work: Path, label: str, video: Path, original: bytes) -> bool:
    out_dir = work / f"out_{label.replace(' ', '_')}"
    out_dir.mkdir(exist_ok=True)
    got = decode_video_to_file(video, out_dir).output_path.read_bytes()
    ok = got == original
    print(f"  {label:<16} {'ok' if ok else 'FAIL'}   {len(got)} bytes")
    return ok


def main() -> int:
    random.seed(11)
    payload = bytes(random.getrandbits(8) for _ in range(20000))

    with tempfile.TemporaryDirectory(prefix="storeii-selftest-") as tmp:
        work = Path(tmp)
        src = work / "original.bin"
        src.write_bytes(payload)

        native = work / "native.avi"
        result = encode_file_to_video(src, native)

        _reencode(native, work / "half.avi", 0.5)
        _reencode(native, work / "third.avi", 1 / 3)
        _repeat(native, work / "dup.avi", 4)

        small = work / "small.avi"
        encode_file_to_video(src, small, width=1280, height=720)
        _reencode(small, work / "small_half.avi", 0.5)

        print(f"encoded {len(payload)} bytes -> {result.frame_count} frames")
        failures = 0
        for label, video in (
            ("native", native),
            ("half size", work / "half.avi"),
            ("third size", work / "third.avi"),
            ("4x repeated", work / "dup.avi"),
            ("custom half", work / "small_half.avi"),
        ):
            failures += not _check(work, label, video, payload)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
