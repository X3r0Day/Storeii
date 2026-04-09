from __future__ import annotations

import math
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from reedsolo import RSCodec, ReedSolomonError

DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
DEFAULT_FRAME_RATE = 1
DEFAULT_PIXEL_SIZE = 20
DEFAULT_VIDEO_EXTENSION = ".avi"

_HEADER_MAGIC = b"STI2"
_HEADER_STRUCT = struct.Struct(">4sIQ")
_ECC_SYMBOLS = 30


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


def encode_file_to_video(
    input_path: str | Path,
    output_video_path: str | Path,
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    frame_rate: int = DEFAULT_FRAME_RATE,
    pix: int = DEFAULT_PIXEL_SIZE,
) -> EncodeResult:
    input_path = Path(input_path)
    output_video_path = Path(output_video_path)
    output_video_path.parent.mkdir(parents=True, exist_ok=True)

    _, _, _, chunk_bytes = _validate_geometry(width, height, pix)

    # ZIP compression + Reed-Solomon Error Correction
    raw_data = input_path.read_bytes()
    compressed_data = zlib.compress(raw_data, level=9)
    rs = RSCodec(_ECC_SYMBOLS)
    encoded_payload = bytes(rs.encode(compressed_data))

    filename_bytes = input_path.name.encode("utf-8")
    if len(filename_bytes) > 2**32 - 1:
        raise StoreiiError("Filename metadata is too large to embed.")

    header = _HEADER_STRUCT.pack(_HEADER_MAGIC, len(filename_bytes), len(encoded_payload)) + filename_bytes

    total_payload_bytes = len(header) + len(encoded_payload)
    frame_count = max(1, math.ceil(total_payload_bytes / chunk_bytes))

    writer = cv2.VideoWriter(
        str(output_video_path),
        cv2.VideoWriter_fourcc(*"png "),
        frame_rate,
        (width, height),
        isColor=False,
    )
    if not writer.isOpened():
        raise StoreiiError(
            "OpenCV could not open the output video writer. Ensure the PNG codec is available."
        )

    frames_written = 0
    try:
        buffer = bytearray(header + encoded_payload)
        
        while buffer:
            frame_bytes = bytes(buffer[:chunk_bytes])
            del buffer[:chunk_bytes]
            if len(frame_bytes) < chunk_bytes:
                frame_bytes += b"\x00" * (chunk_bytes - len(frame_bytes))

            frame = _frame_from_bytes(frame_bytes, width, height, pix)
            writer.write(frame)
            frames_written += 1
    finally:
        writer.release()

    return EncodeResult(
        output_path=output_video_path,
        input_name=input_path.name,
        payload_bytes=total_payload_bytes,
        frame_count=frames_written,
        video_bytes=output_video_path.stat().st_size,
    )


def _extract_payload(video_path: Path, pix: int) -> bytes:
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise InvalidVideoError("OpenCV could not open the video file.")

    try:
        ok, first_frame = capture.read()
        if not ok:
            raise InvalidVideoError("The video file does not contain any decodable frames.")

        height, width = first_frame.shape[:2]
        reduced_width, reduced_height, _, _ = _validate_geometry(width, height, pix)

        payload = bytearray()
        payload.extend(_threshold_frame(first_frame, reduced_width, reduced_height))

        while True:
            ok, frame = capture.read()
            if not ok:
                break
            payload.extend(_threshold_frame(frame, reduced_width, reduced_height))

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
        
    encoded_data = payload[name_end:data_end]
    
    # Reed-Solomon Decode + ZIP Decompress
    try:
        rs = RSCodec(_ECC_SYMBOLS)
        compressed_data = bytes(rs.decode(encoded_data)[0])
        file_bytes = zlib.decompress(compressed_data)
    except (ReedSolomonError, zlib.error) as exc:
        raise InvalidVideoError("Failed to decode or decompress payload data. The video might be corrupted.") from exc

    return _safe_filename(filename, fallback_filename), file_bytes


def _decode_legacy_video(
    video_path: Path,
    output_dir: Path,
    output_filename: str,
    pix: int,
) -> DecodeResult:
    # (Keep legacy video decode untouched for backward compatibility)
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
) -> DecodeResult:
    video_path = Path(video_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    fallback_name = output_filename or f"{video_path.stem}.bin"
    payload = _extract_payload(video_path, pix)

    try:
        filename, file_bytes = _parse_payload(payload, fallback_name)
    except InvalidVideoError:
        return _decode_legacy_video(video_path, output_dir, fallback_name, pix)

    output_path = output_dir / _safe_filename(output_filename or filename, fallback_name)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(file_bytes)

    return DecodeResult(
        output_path=output_path,
        original_filename=output_path.name,
        recovered_bytes=len(file_bytes),
        legacy_format=False,
    )
