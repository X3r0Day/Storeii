from __future__ import annotations

import math
import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from reedsolo import RSCodec, ReedSolomonError

DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
DEFAULT_FRAME_RATE = 25
DEFAULT_PIXEL_SIZE = 8
DEFAULT_VIDEO_EXTENSION = ".avi"

# v2 file header, kept around so old videos still decode
_HEADER_MAGIC = b"STI2"
_HEADER_STRUCT = struct.Struct(">4sIQ")
_ECC_SYMBOLS = 30

# v3 layout. Each frame tags itself (so frames can be reordered or de-duplicated after a
# transcoder messes with timing), and the file header stores the logical grid rather than
# trusting whatever resolution the video comes back at.
_FILE_MAGIC = b"STI3"
_FILE_HEADER = struct.Struct(">4sHHIIH")  # magic, grid_w, grid_h, frames, payload_len, name_len
_FRAME_MAGIC = 0x5746
_FRAME_HEADER = struct.Struct(">HH")      # magic, seq

# Block sizes to try against an incoming resolution. The default grid is probed first, so
# the common 1080p/720p/480p case costs a single decode attempt.
_BLOCK_SIZES = (
    4, 5, 6, 8, 10, 12, 16, 20, 24, 25, 30, 32, 40,
    48, 50, 60, 64, 80, 96, 100, 120, 128, 160, 192, 200, 240,
)
_GRID_SWEEP_LIMIT = 256

# called as (done, total); total is 0 when the count isn't known yet
Progress = Callable[[int, int], None]


class StoreiiError(Exception):
    """Base error for Storeii codec operations."""


class InvalidVideoError(StoreiiError):
    """Raised when a video cannot be decoded as Storeii data."""


@dataclass(slots=True)
class EncodeResult:
    output_path: Path
    input_name: str
    payload_bytes: int
    frame_count: int
    video_bytes: int


@dataclass(slots=True)
class DecodeResult:
    output_path: Path
    original_filename: str
    recovered_bytes: int
    legacy_format: bool


def _validate_geometry(width: int, height: int, pix: int) -> tuple[int, int, int, int]:
    if width <= 0 or height <= 0:
        raise StoreiiError("Video dimensions must be positive integers.")
    if pix <= 0:
        raise StoreiiError("Pixel size must be a positive integer.")
    if width % pix != 0 or height % pix != 0:
        raise StoreiiError("Width and height must be divisible by pix.")

    reduced_width = width // pix
    reduced_height = height // pix
    chunk_bits = reduced_width * reduced_height
    if chunk_bits % 8 != 0:
        raise StoreiiError(
            "The reduced frame size must contain a multiple of 8 pixels so bytes align cleanly."
        )

    return reduced_width, reduced_height, chunk_bits, chunk_bits // 8


def _safe_filename(filename: str, fallback: str) -> str:
    name = Path(filename).name.strip()
    if not name or name in {".", ".."}:
        return fallback
    return name


def _frame_from_bytes(frame_bytes: bytes, width: int, height: int, pix: int) -> np.ndarray:
    reduced_width, reduced_height, _, chunk_bytes = _validate_geometry(width, height, pix)
    if len(frame_bytes) != chunk_bytes:
        raise StoreiiError("Frame payload size does not match the configured geometry.")

    bits = np.unpackbits(np.frombuffer(frame_bytes, dtype=np.uint8))
    frame = (bits.reshape((reduced_height, reduced_width)) * 255).astype(np.uint8)
    return cv2.resize(frame, (width, height), interpolation=cv2.INTER_NEAREST)


def _threshold_frame(frame: np.ndarray, reduced_width: int, reduced_height: int) -> bytes:
    if frame.ndim == 3:
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    frame = cv2.resize(frame, (reduced_width, reduced_height), interpolation=cv2.INTER_NEAREST)
    bits = (frame > 127).astype(np.uint8).reshape(-1)
    return np.packbits(bits).tobytes()


def _frame_to_bits(frame: np.ndarray, grid_w: int, grid_h: int) -> bytes:
    # INTER_AREA averages each source block into one grid cell; INTER_NEAREST samples a
    # single pixel and lands off-grid as soon as the video has been rescaled.
    if frame.ndim == 3:
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(frame, (grid_w, grid_h), interpolation=cv2.INTER_AREA)
    return np.packbits((small > 127).astype(np.uint8).reshape(-1)).tobytes()


def _compress_payload(raw: bytes) -> bytes:
    compressed = zlib.compress(raw, level=9)
    return bytes(RSCodec(_ECC_SYMBOLS).encode(compressed))


def _decompress_payload(encoded: bytes) -> bytes:
    try:
        compressed = bytes(RSCodec(_ECC_SYMBOLS).decode(encoded)[0])
        return zlib.decompress(compressed)
    except (ReedSolomonError, zlib.error) as exc:
        raise InvalidVideoError("Failed to decode or decompress payload data.") from exc


def encode_file_to_video(
    input_path: str | Path,
    output_video_path: str | Path,
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    frame_rate: int = DEFAULT_FRAME_RATE,
    pix: int = DEFAULT_PIXEL_SIZE,
    progress: Progress | None = None,
) -> EncodeResult:
    input_path = Path(input_path)
    output_video_path = Path(output_video_path)
    output_video_path.parent.mkdir(parents=True, exist_ok=True)

    grid_w, grid_h, _, chunk_bytes = _validate_geometry(width, height, pix)
    data_per_frame = chunk_bytes - _FRAME_HEADER.size
    if data_per_frame <= _FILE_HEADER.size:
        raise StoreiiError("Frame is too small to hold the Storeii file header.")

    raw_data = input_path.read_bytes()
    encoded_payload = _compress_payload(raw_data)
    filename_bytes = input_path.name.encode("utf-8")
    if len(filename_bytes) > 2**16 - 1:
        raise StoreiiError("Filename metadata is too large to embed.")

    header_len = _FILE_HEADER.size + len(filename_bytes)
    frame_count = max(1, math.ceil((header_len + len(encoded_payload)) / data_per_frame))

    stream = _FILE_HEADER.pack(
        _FILE_MAGIC, grid_w, grid_h, frame_count, len(encoded_payload), len(filename_bytes)
    ) + filename_bytes + encoded_payload

    writer = cv2.VideoWriter(
        str(output_video_path),
        cv2.VideoWriter_fourcc(*"png "),
        frame_rate,
        (width, height),
        isColor=False,
    )
    if not writer.isOpened():
        raise StoreiiError(
            "OpenCV could not open the output video writer."
        )

    written = 0
    try:
        for seq in range(frame_count):
            chunk = stream[seq * data_per_frame : (seq + 1) * data_per_frame]
            if len(chunk) < data_per_frame:
                chunk += b"\x00" * (data_per_frame - len(chunk))
            writer.write(_frame_from_bytes(_FRAME_HEADER.pack(_FRAME_MAGIC, seq) + chunk, width, height, pix))
            written += 1
            if progress is not None:
                progress(written, frame_count)
    finally:
        writer.release()

    return EncodeResult(
        output_path=output_video_path,
        input_name=input_path.name,
        payload_bytes=len(stream),
        frame_count=written,
        video_bytes=output_video_path.stat().st_size,
    )


def _candidate_grids(frame_w: int, frame_h: int) -> list[tuple[int, int]]:
    found: list[tuple[int, int]] = []

    def take(gw: int, gh: int) -> None:
        if gw < 1 or gh < 1 or (gw * gh) % 8:
            return
        if (gw, gh) not in found:
            found.append((gw, gh))

    take(DEFAULT_WIDTH // DEFAULT_PIXEL_SIZE, DEFAULT_HEIGHT // DEFAULT_PIXEL_SIZE)

    for block in _BLOCK_SIZES:
        if frame_w % block == 0 and frame_h % block == 0:
            take(frame_w // block, frame_h // block)

    # aspect-preserving sweep; only reached for encodes that don't match a block size above
    for gw in range(8, _GRID_SWEEP_LIMIT + 1):
        take(gw, round(gw * frame_h / frame_w))

    return found


def _probe_grid(frame: np.ndarray) -> tuple[int, int] | None:
    for gw, gh in _candidate_grids(frame.shape[1], frame.shape[0]):
        bits = _frame_to_bits(frame, gw, gh)
        if len(bits) < _FRAME_HEADER.size + _FILE_HEADER.size:
            continue
        magic, seq = _FRAME_HEADER.unpack_from(bits)
        if magic != _FRAME_MAGIC or seq != 0:
            continue
        fmagic, fgw, fgh, _, _, _ = _FILE_HEADER.unpack_from(bits, _FRAME_HEADER.size)
        if fmagic == _FILE_MAGIC and (fgw, fgh) == (gw, gh):
            return gw, gh
    return None


def _parse_v3(stream: bytes) -> tuple[str, bytes]:
    if len(stream) < _FILE_HEADER.size:
        raise InvalidVideoError("Storeii payload too short")

    magic, _gw, _gh, _frames, payload_len, name_len = _FILE_HEADER.unpack_from(stream)
    if magic != _FILE_MAGIC:
        raise InvalidVideoError("missing Storeii v3 header")

    name_start = _FILE_HEADER.size
    name_end = name_start + name_len
    data_end = name_end + payload_len
    if len(stream) < data_end:
        raise InvalidVideoError("Storeii v3 payload is truncated")

    try:
        filename = stream[name_start:name_end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidVideoError("filename metadata is corrupt") from exc

    return _safe_filename(filename, "recovered.bin"), _decompress_payload(stream[name_end:data_end])


def _decode_v3(video_path: Path, progress: Progress | None = None) -> tuple[str, bytes]:
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise InvalidVideoError("could not open video")

    try:
        ok, first = capture.read()
        if not ok:
            raise InvalidVideoError("video has no frames")

        grid = _probe_grid(first)
        if grid is None:
            raise InvalidVideoError("no Storeii v3 frame tag")
        grid_w, grid_h = grid
        per_frame = (grid_w * grid_h) // 8 - _FRAME_HEADER.size

        # seq 0 carries the file header, so we can read the frame count up front and ignore
        # any seq beyond it (a stray tag byte can otherwise look like a huge index)
        frame_count = _FILE_HEADER.unpack_from(_frame_to_bits(first, grid_w, grid_h), _FRAME_HEADER.size)[3]

        chunks: dict[int, bytes] = {}

        def consume(frame: np.ndarray) -> None:
            bits = _frame_to_bits(frame, grid_w, grid_h)
            if len(bits) < _FRAME_HEADER.size:
                return
            magic, seq = _FRAME_HEADER.unpack_from(bits)
            if magic != _FRAME_MAGIC or seq >= frame_count:
                return
            chunks.setdefault(seq, bits[_FRAME_HEADER.size : _FRAME_HEADER.size + per_frame])
            if progress is not None:
                progress(len(chunks), frame_count)

        consume(first)
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            consume(frame)
    finally:
        capture.release()

    if not chunks:
        raise InvalidVideoError("no tagged frames survived")

    stream = bytearray()
    for seq in range(max(chunks) + 1):
        stream += chunks.get(seq, b"\x00" * per_frame)

    return _parse_v3(bytes(stream))


def _extract_payload(video_path: Path, pix: int, progress: Progress | None = None) -> bytes:
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise InvalidVideoError("OpenCV could not open the video file.")

    try:
        ok, first_frame = capture.read()
        if not ok:
            raise InvalidVideoError("The video file does not contain any decodable frames.")

        height, width = first_frame.shape[:2]
        reduced_width, reduced_height, _, _ = _validate_geometry(width, height, pix)

        frames = 1
        payload = bytearray()
        payload.extend(_threshold_frame(first_frame, reduced_width, reduced_height))

        while True:
            ok, frame = capture.read()
            if not ok:
                break
            payload.extend(_threshold_frame(frame, reduced_width, reduced_height))
            frames += 1
            if progress is not None and frames % 32 == 0:
                progress(frames, 0)

        return bytes(payload)
    finally:
        capture.release()


def _parse_payload(payload: bytes, fallback_filename: str) -> tuple[str, bytes]:
    if len(payload) < _HEADER_STRUCT.size:
        raise InvalidVideoError("Decoded payload is too short to contain metadata.")

    magic, name_length, encoded_size = _HEADER_STRUCT.unpack(payload[: _HEADER_STRUCT.size])
    if magic != _HEADER_MAGIC:
        raise InvalidVideoError("The video does not contain a Storeii v2 payload header.")

    name_start = _HEADER_STRUCT.size
    name_end = name_start + name_length
    data_end = name_end + encoded_size
    if len(payload) < data_end:
        raise InvalidVideoError("The embedded file data is truncated.")

    raw_name = payload[name_start:name_end]
    try:
        filename = raw_name.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidVideoError("The embedded filename metadata is corrupted.") from exc

    return _safe_filename(filename, fallback_filename), _decompress_payload(payload[name_end:data_end])


def _decode_legacy_video(
    video_path: Path,
    output_dir: Path,
    output_filename: str,
    pix: int,
    progress: Progress | None = None,
) -> DecodeResult:
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise InvalidVideoError("OpenCV could not open the video file.")

    output_path = output_dir / _safe_filename(output_filename, f"{video_path.stem}.bin")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        ok, first_frame = capture.read()
        if not ok:
            raise InvalidVideoError("The video file does not contain any decodable frames.")

        height, width = first_frame.shape[:2]
        reduced_width, reduced_height, _, _ = _validate_geometry(width, height, pix)

        recovered_bytes = 0
        with output_path.open("wb") as output_file:
            frame_index = 0
            frame = first_frame

            while True:
                frame_bytes = _threshold_frame(frame, reduced_width, reduced_height)
                bits = np.unpackbits(np.frombuffer(frame_bytes, dtype=np.uint8))

                if frame_index == 0:
                    one_indices = np.flatnonzero(bits)
                    if one_indices.size == 0:
                        raise InvalidVideoError("Legacy Storeii marker bit was not found.")
                    bits = bits[one_indices[0] + 1 :]

                if bits.size:
                    chunk = np.packbits(bits).tobytes()
                    output_file.write(chunk)
                    recovered_bytes += len(chunk)

                frame_index += 1
                if progress is not None and frame_index % 32 == 0:
                    progress(frame_index, 0)
                ok, frame = capture.read()
                if not ok:
                    break

        return DecodeResult(
            output_path=output_path,
            original_filename=output_path.name,
            recovered_bytes=recovered_bytes,
            legacy_format=True,
        )
    finally:
        capture.release()


def decode_video_to_file(
    video_path: str | Path,
    output_dir: str | Path,
    *,
    output_filename: str | None = None,
    pix: int = DEFAULT_PIXEL_SIZE,
    progress: Progress | None = None,
) -> DecodeResult:
    video_path = Path(video_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    fallback_name = output_filename or f"{video_path.stem}.bin"

    try:
        filename, file_bytes = _decode_v3(video_path, progress)
    except InvalidVideoError:
        try:
            filename, file_bytes = _parse_payload(_extract_payload(video_path, pix, progress), fallback_name)
        except InvalidVideoError:
            return _decode_legacy_video(video_path, output_dir, fallback_name, pix, progress)

    output_path = output_dir / _safe_filename(output_filename or filename, fallback_name)
    output_path.write_bytes(file_bytes)

    return DecodeResult(
        output_path=output_path,
        original_filename=output_path.name,
        recovered_bytes=len(file_bytes),
        legacy_format=False,
    )
