"""Videos attached to flights: upload, metadata, range streaming, sync settings, limits."""

from __future__ import annotations

import hashlib
import shutil
import struct
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flightlens import media
from flightlens.api import create_app
from flightlens.demo import seed

PICK = "quad-01-healthy.bin"


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def tiny_mp4(seconds: float = 12.5, created: int = 3_860_000_000, filler: int = 200_000) -> bytes:
    """A minimal MP4: ftyp, a moov with mvhd (duration, creation time) and a tkhd, and data."""
    mvhd = (
        bytes(4) + struct.pack(">IIII", created, created, 1000, round(seconds * 1000)) + bytes(80)
    )
    tkhd = bytes(76) + struct.pack(">II", 1280 << 16, 720 << 16)
    moov = box(b"moov", box(b"mvhd", mvhd) + box(b"trak", box(b"tkhd", tkhd)))
    data = bytes(range(256)) * (filler // 256)
    return box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2mp41") + moov + box(b"mdat", data)


@pytest.fixture(scope="module")
def library(tmp_path_factory: pytest.TempPathFactory) -> Path:
    data = tmp_path_factory.mktemp("lib")
    seed(data, only=[PICK])
    return data


@pytest.fixture
def client(library: Path, tmp_path: Path) -> Iterator[TestClient]:
    data = tmp_path / "data"
    shutil.copytree(library, data)
    with TestClient(create_app(data, max_video_bytes=2 * 1024**2), base_url="http://localhost") as c:
        yield c


def flight_id(client: TestClient) -> str:
    return str(client.get("/api/flights").json()["flights"][0]["id"])


def upload(client: TestClient, fid: str, name: str, content: bytes) -> object:
    return client.post(f"/api/flights/{fid}/videos", files={"file": (name, content)})


def test_upload_list_stream_and_delete(client: TestClient) -> None:
    fid = flight_id(client)
    content = tiny_mp4()
    r = upload(client, fid, "onboard camera.mp4", content)
    assert r.status_code == 201, r.text  # type: ignore[attr-defined]
    video = r.json()["video"]  # type: ignore[attr-defined]
    assert video["sha256"] == hashlib.sha256(content).hexdigest()
    assert video["filename"] == "onboard camera.mp4" and video["file"].endswith(".mp4")
    if media.ffprobe() is None:  # the header alone gives these
        assert video["duration_s"] == 12.5 and (video["width"], video["height"]) == (1280, 720)
    assert video["sync_method"] == "none" and video["offset_s"] is None and video["rate"] == 1.0

    # the same file again is recognised
    again = upload(client, fid, "copy.mp4", content).json()  # type: ignore[attr-defined]
    assert again["duplicate"] and again["video"]["id"] == video["id"]
    assert [v["id"] for v in client.get(f"/api/flights/{fid}/videos").json()["videos"]] == [
        video["id"]
    ]

    # streaming answers Range requests, so the player can seek without the whole file
    url = f"/api/videos/{video['id']}/file"
    full = client.get(url)
    assert full.status_code == 200 and full.content == content
    assert full.headers["content-type"] == "video/mp4"
    part = client.get(url, headers={"Range": "bytes=1000-1999"})
    assert part.status_code == 206 and part.content == content[1000:2000]
    assert part.headers["content-range"] == f"bytes 1000-1999/{len(content)}"

    assert client.delete(f"/api/videos/{video['id']}").status_code == 200
    assert client.get(url).status_code == 404
    assert client.get(f"/api/flights/{fid}/videos").json()["videos"] == []


def test_sync_settings(client: TestClient) -> None:
    fid = flight_id(client)
    vid = upload(client, fid, "v.mp4", tiny_mp4()).json()["video"]["id"]  # type: ignore[attr-defined]
    r = client.patch(
        f"/api/videos/{vid}",
        json={"offset_s": 7.3, "rate": 1.001, "sync_method": "points",
              "sync_points": [[2.0, 9.302], [10.0, 17.31]], "label": "onboard"},
    )  # fmt: skip
    assert r.status_code == 200, r.text
    v = r.json()["video"]
    assert (v["offset_s"], v["rate"], v["sync_method"]) == (7.3, 1.001, "points")
    assert v["sync_points"] == [[2.0, 9.302], [10.0, 17.31]] and v["label"] == "onboard"
    # only what was sent changes
    v = client.patch(f"/api/videos/{vid}", json={"label": "nose cam"}).json()["video"]
    assert v["offset_s"] == 7.3 and v["label"] == "nose cam"
    for bad in (
        {"rate": 2.0},
        {"sync_method": "magic"},
        {"sync_points": [[0, 0]] * 51},
        {"duration_s": -1},
    ):
        assert client.patch(f"/api/videos/{vid}", json=bad).status_code == 422, bad
    assert client.patch("/api/videos/nope", json={"label": "x"}).status_code == 404


def test_refusals(client: TestClient, library: Path, tmp_path: Path) -> None:
    fid = flight_id(client)
    r = upload(client, fid, "clip.mov", b"\x00\x00\x00\x14qt  " + bytes(100))
    assert r.status_code == 415 and "ffmpeg" in r.json()["detail"]  # type: ignore[attr-defined]
    mkv = b"\x1a\x45\xdf\xa3" + b"\x42\x82\x88matroska" + bytes(100)
    r = upload(client, fid, "clip.mkv", mkv)
    assert r.status_code == 415 and "Matroska" in r.json()["detail"]  # type: ignore[attr-defined]
    assert upload(client, fid, "empty.mp4", b"").status_code == 400  # type: ignore[attr-defined]
    # over the video limit (2 MB here): refused before the body is read
    r = upload(client, fid, "big.mp4", tiny_mp4(filler=4_000_000))
    assert r.status_code == 413 and "2 MB" in r.json()["detail"]  # type: ignore[attr-defined]
    assert upload(client, "nope", "v.mp4", tiny_mp4()).status_code == 404  # type: ignore[attr-defined]
    # cross-site upload
    r = client.post(f"/api/flights/{fid}/videos", files={"file": ("v.mp4", tiny_mp4())},
                    headers={"Origin": "http://evil.example"})  # fmt: skip
    assert r.status_code == 403

    # low disk: refused before the body is read
    low = tmp_path / "low"
    shutil.copytree(library, low)
    with TestClient(create_app(low, min_free_bytes=1 << 62), base_url="http://localhost") as c:
        assert upload(c, fid, "v.mp4", tiny_mp4()).status_code == 507  # type: ignore[attr-defined]
    # read-only server
    ro = tmp_path / "ro"
    shutil.copytree(library, ro)
    with TestClient(create_app(ro, read_only=True), base_url="http://localhost") as c:
        assert upload(c, fid, "v.mp4", tiny_mp4()).status_code == 403  # type: ignore[attr-defined]


def test_deleting_the_flight_removes_its_videos(client: TestClient) -> None:
    fid = flight_id(client)
    vid = upload(client, fid, "v.mp4", tiny_mp4()).json()["video"]["id"]  # type: ignore[attr-defined]
    store = client.app.state.store  # type: ignore[attr-defined]
    path = store.video_path(store.get_video(vid))
    assert path.exists()
    assert client.delete(f"/api/flights/{fid}").status_code == 200
    assert not path.exists() and store.get_video(vid) is None


def test_header_reader_and_type_sniffing(tmp_path: Path) -> None:
    p = tmp_path / "v.mp4"
    p.write_bytes(tiny_mp4(seconds=42.0, created=3_860_000_000))
    info = media._mp4_header(p)
    assert info.duration_s == 42.0 and (info.width, info.height) == (1280, 720)
    assert info.created_at_media is not None and info.created_at_media.startswith("2026-")
    p.write_bytes(tiny_mp4(created=0))  # an unset camera clock is no information
    assert media._mp4_header(p).created_at_media is None
    p.write_bytes(b"\x1a\x45\xdf\xa3\x01\x00\x00\x00webm" + bytes(50))
    assert media.sniff(p) == "webm"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_videos_are_probed(tmp_path: Path) -> None:
    mp4, webm = tmp_path / "a.mp4", tmp_path / "a.webm"
    src = ["-f", "lavfi", "-i", "testsrc=size=320x240:rate=25", "-t", "2"]
    h264 = ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    stamp = ["-metadata", "creation_time=2026-05-04T09:35:00Z"]
    subprocess.run(["ffmpeg", "-v", "error", "-y", *src, *h264, *stamp, str(mp4)], check=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", *src, "-c:v", "libvpx-vp9", str(webm)], check=True
    )
    a = media.probe(mp4)
    assert (a.width, a.height, a.fps) == (320, 240, 25.0) and abs((a.duration_s or 0) - 2) < 0.1
    assert a.created_at_media == "2026-05-04T09:35:00+00:00"
    if media.ffprobe():
        assert a.codec == "h264" and media.probe(webm).codec == "vp9"
