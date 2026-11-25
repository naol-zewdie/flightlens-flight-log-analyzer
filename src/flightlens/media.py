"""Videos attached to flights: what the file is, and what is in it.

FlightLens accepts the two formats browsers play natively, MP4 (H.264/AAC)
and WebM (VP8/VP9/AV1), recognised by their first bytes, never by the file
name. Metadata (duration, size, codec, frame rate, creation time) comes from
``ffprobe`` when it is installed (the Docker image has it); without it, an
MP4's own header gives duration, size and creation time, and the browser
reports a WebM's duration when it first plays it.

The original file is stored as uploaded and never changed.
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

MEDIA_TYPES = {"mp4": "video/mp4", "webm": "video/webm"}
BROWSER_CODECS = {"h264", "vp8", "vp9", "av1"}
CONVERT_HINT = "Convert it first, for example: ffmpeg -i input.mov -c:v libx264 -c:a aac output.mp4"
MP4_EPOCH = datetime(1904, 1, 1, tzinfo=UTC)


class MediaError(ValueError):
    pass


@dataclass
class VideoInfo:
    duration_s: float | None = None
    width: int | None = None
    height: int | None = None
    codec: str | None = None
    fps: float | None = None
    created_at_media: str | None = None
    """Container creation time, UTC ISO 8601. Cameras often get this wrong."""

    def as_row(self) -> dict[str, Any]:
        return asdict(self)


def sniff(path: Path) -> str:
    """``mp4`` or ``webm`` from the file's first bytes; MediaError otherwise."""
    with path.open("rb") as fh:
        head = fh.read(4096)
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":  # EBML: Matroska or WebM
        if b"webm" in head[:64]:
            return "webm"
        raise MediaError(f"Matroska (.mkv) video is not played by browsers. {CONVERT_HINT}")
    raise MediaError(f"Not an MP4 or WebM video. {CONVERT_HINT}")


def ffprobe() -> str | None:
    return os.environ.get("FLIGHTLENS_FFPROBE") or shutil.which("ffprobe")


def probe(path: Path) -> VideoInfo:
    exe = ffprobe()
    if exe:
        try:
            return _ffprobe(exe, path)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError):
            pass  # fall back to what the container header says
    if sniff(path) == "mp4":
        return _mp4_header(path)
    return VideoInfo()


def _ffprobe(exe: str, path: Path) -> VideoInfo:
    out = subprocess.run(
        [exe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True,
        timeout=60,
        check=True,
    ).stdout
    data = json.loads(out)
    fmt = data.get("format", {})
    video: dict[str, Any] = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"), {}
    )
    fps = None
    rate = video.get("avg_frame_rate") or video.get("r_frame_rate")
    if rate and "/" in rate:
        num, den = (float(x) for x in rate.split("/"))
        fps = round(num / den, 3) if den else None
    created = (fmt.get("tags") or {}).get("creation_time") or (video.get("tags") or {}).get(
        "creation_time"
    )
    return VideoInfo(
        duration_s=float(fmt["duration"]) if fmt.get("duration") else None,
        width=video.get("width"),
        height=video.get("height"),
        codec=video.get("codec_name"),
        fps=fps,
        created_at_media=_iso(created),
    )


def _iso(text: str | None) -> str | None:
    if not text:
        return None
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    if when.year < 1990:  # an unset clock (1904 or 1970) is no information
        return None
    return when.astimezone(UTC).isoformat()


def _boxes(data: bytes, start: int, end: int) -> list[tuple[bytes, int, int]]:
    """(type, payload start, payload end) of the ISO-BMFF boxes in data[start:end]."""
    out = []
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", data[pos : pos + 8])
        header = 8
        if size == 1 and pos + 16 <= end:
            size = struct.unpack(">Q", data[pos + 8 : pos + 16])[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            break
        out.append((kind, pos + header, pos + size))
        pos += size
    return out


def _mp4_header(path: Path) -> VideoInfo:
    """Duration, size and creation time from an MP4's moov box (no ffprobe needed)."""
    size = path.stat().st_size
    with path.open("rb") as fh:
        data = fh.read(min(size, 64 * 1024**2))  # moov is at the start or the end
        if b"moov" not in data and size > len(data):
            fh.seek(max(0, size - 64 * 1024**2))
            data = fh.read()
    info = VideoInfo(codec=None)
    moov = next((b for b in _boxes(data, 0, len(data)) if b[0] == b"moov"), None)
    if moov is None:
        return info
    for kind, a, b in _boxes(data, moov[1], moov[2]):
        if kind == b"mvhd":
            version = data[a]
            if version == 1:
                created, _, scale, duration = struct.unpack(">QQIQ", data[a + 4 : a + 32])
            else:
                created, _, scale, duration = struct.unpack(">IIII", data[a + 4 : a + 20])
            if scale:
                info.duration_s = duration / scale
            if created:
                when = MP4_EPOCH + timedelta(seconds=created)
                info.created_at_media = _iso(when.isoformat())
        elif kind == b"trak" and info.width is None:
            tkhd = next((x for x in _boxes(data, a, b) if x[0] == b"tkhd"), None)
            if tkhd is not None:
                tb = tkhd[2]
                w, h = struct.unpack(">II", data[tb - 8 : tb])
                if w and h:
                    info.width, info.height = w >> 16, h >> 16
    return info
