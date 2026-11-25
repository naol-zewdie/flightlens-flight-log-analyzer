"""End-to-end: the Replay tab in a real browser (Playwright).

Runs against a real server on a seeded library and the built web UI
(``web/dist``). Uses Playwright's Chromium when installed
(``uv run playwright install chromium``), else the system's Microsoft Edge or
Chrome; skipped when there is no browser or no built UI.
"""

from __future__ import annotations

import hashlib
import shutil
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pw = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).parent.parent
WEB = ROOT / "web" / "dist"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, dict[str, str]]]:
    if not (WEB / "index.html").exists():
        pytest.skip("web UI not built (cd web && npm run build)")
    import uvicorn

    from flightlens.api import create_app
    from flightlens.demo import seed
    from flightlens.sim.manoeuvre import manoeuvre_log
    from flightlens.store import Store
    from flightlens.worker import process_log

    data = tmp_path_factory.mktemp("lib")
    seed(data, only=["plane-04-crash.bin"])
    store = Store(data)
    src = manoeuvre_log(tmp_path_factory.mktemp("m") / "manoeuvres.bin")
    upload = data / "uploads" / "manoeuvres.bin"
    shutil.copy(src, upload)
    created = store.create_flight(
        upload, hashlib.sha256(src.read_bytes()).hexdigest(), "manoeuvres.bin", "dataflash"
    )
    assert created is not None
    fid, job = created
    store.record_result(fid, process_log(str(store.original_path(fid)), str(store.flight_dir(fid))))
    store.set_job(job, "done")
    ids = {r["filename"]: r["id"] for r in store.list_flights()}

    port = _free_port()
    config = uvicorn.Config(create_app(data, WEB), host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 60
    while not srv.started and time.time() < deadline:
        time.sleep(0.1)
    yield f"http://localhost:{port}", ids
    srv.should_exit = True
    thread.join(10)


# CI runners have no GPU, and Chrome no longer falls back to software WebGL by itself
SOFTWARE_GL = ["--enable-unsafe-swiftshader", "--use-angle=swiftshader"]


def _launch(p: Any, args: list[str]) -> Any:
    last: Exception | None = None
    for kwargs in ({}, {"channel": "msedge"}, {"channel": "chrome"}):
        try:
            return p.chromium.launch(args=args, **kwargs)
        except Exception as exc:  # browser not installed
            last = exc
    pytest.skip(f"no browser for Playwright: {last}")


@pytest.fixture(scope="module")
def playwright() -> Iterator[Any]:
    with pw.sync_playwright() as p:  # only one may run at a time
        yield p


@pytest.fixture(scope="module")
def browser(playwright: Any) -> Iterator[Any]:
    b = _launch(playwright, SOFTWARE_GL)
    yield b
    b.close()


def _open(browser: Any, url: str) -> Any:
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: m.type == "error" and errors.append(m.text))
    page.goto(url, wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_selector(".scene3d canvas", timeout=120_000)
    page.errors = errors
    return page


def _time(page: Any) -> float:
    text = page.locator(".time-readout").inner_text().split("/")[0].strip()
    mm, rest = text.split(":")
    return int(mm) * 60 + float(rest)


def test_replay_opens_at_t_and_shows_the_attitude(server: Any, browser: Any) -> None:
    base, ids = server
    page = _open(browser, f"{base}/flights/{ids['manoeuvres.bin']}/replay?t=22")
    assert abs(_time(page) - 22) < 0.2
    # 22 s into the script the quad holds a 30° roll
    label = page.locator(".horizon").get_attribute("aria-label")
    assert label == "Roll 30 degrees, pitch 0 degrees", label
    assert "Loiter" in page.locator(".hud-grid").inner_text()
    assert page.errors == []
    page.close()


def test_play_pause_keys_and_speed(server: Any, browser: Any) -> None:
    base, ids = server
    page = _open(browser, f"{base}/flights/{ids['manoeuvres.bin']}/replay?t=50")
    started = time.monotonic()
    page.keyboard.press("Space")
    page.wait_for_timeout(1500)
    page.keyboard.press("k")
    elapsed = time.monotonic() - started  # includes the key presses' own round trips
    t1 = _time(page)
    # played at 1x: about the wait, never more than the wall time it had
    assert 50 + 1.0 <= t1 <= 50 + elapsed + 0.3, (t1, elapsed)
    page.wait_for_timeout(600)
    assert _time(page) == t1, "paused"
    page.keyboard.press("l")  # faster
    assert page.locator(".rate select").input_value() == "2"
    page.keyboard.press("Shift+ArrowRight")
    assert abs(_time(page) - (t1 + 10)) < 0.2
    page.keyboard.press("Home")
    assert _time(page) < 0.1
    # the URL keeps the paused time, to share
    page.wait_for_timeout(300)
    assert "t=" in page.url
    page.close()


def test_replay_from_a_finding_and_cameras(server: Any, browser: Any) -> None:
    base, ids = server
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(
        f"{base}/flights/{ids['plane-04-crash.bin']}",
        wait_until="domcontentloaded",
        timeout=120_000,
    )
    link = page.get_by_role("link", name="Replay from here").first
    link.wait_for(timeout=120_000)
    href = link.get_attribute("href")
    assert href is not None and "/replay?t=" in href
    link.click()
    page.wait_for_selector(".scene3d canvas", timeout=120_000)
    wanted = float(href.split("t=")[1])
    assert abs(_time(page) - wanted) < 0.2
    for key, name in (("2", "Orbit"), ("3", "Top"), ("4", "Cockpit"), ("1", "Chase")):
        page.keyboard.press(key)
        pressed = page.get_by_role("button", name=name).get_attribute("aria-pressed")
        assert pressed == "true", name
    # every moment in the list jumps the replay
    page.locator(".moments button").first.click()
    assert page.locator(".moments li.active").count() == 1
    page.close()


def test_theme_switch_keeps_the_replay(server: Any, browser: Any) -> None:
    base, ids = server
    page = _open(browser, f"{base}/flights/{ids['manoeuvres.bin']}/replay?t=60")
    page.get_by_role("button", name="Light").click()
    page.get_by_role("button", name="Dark").click()
    page.wait_for_timeout(300)
    assert page.locator(".scene3d canvas").count() == 1
    assert abs(_time(page) - 60) < 0.2
    assert page.errors == []
    page.close()


def test_without_webgl_the_rest_of_the_replay_works(server: Any, playwright: Any) -> None:
    """A browser without WebGL gets an explanation in the 3D panel, not a broken page."""
    base, ids = server
    b = _launch(playwright, ["--disable-3d-apis", "--disable-gpu"])
    page = b.new_page(viewport={"width": 1280, "height": 900})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}/flights/{ids['manoeuvres.bin']}/replay?t=22",
              wait_until="domcontentloaded", timeout=120_000)  # fmt: skip
    page.wait_for_selector(".scene-fallback", timeout=120_000)
    assert "needs WebGL" in page.locator(".scene-fallback").inner_text()
    label = page.locator(".horizon").get_attribute("aria-label")
    assert label == "Roll 30 degrees, pitch 0 degrees"
    page.keyboard.press("Space")  # playback still works
    page.wait_for_timeout(800)
    assert _time(page) > 22.3
    assert errors == []
    b.close()


# --- video (1.2) ---------------------------------------------------------------------------

# The generated video: it starts 12.3 s into the log, its clock runs 0.1% fast,
# and it flashes white for 0.2 s at three known video times.
OFFSET, RATE = 12.3, 1.001
FLASHES = (10.0, 30.0, 50.0)
FLASH_S = 0.2


def _log_time(video_t: float) -> float:
    return OFFSET + RATE * video_t


@pytest.fixture(scope="module")
def flash_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    import subprocess

    out = tmp_path_factory.mktemp("video") / "flashes.webm"
    on = "+".join(f"between(t,{a},{a + FLASH_S - 0.001})" for a in FLASHES)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=320x180:r=25:d=60",
         "-vf", f"drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='{on}'",
         "-c:v", "libvpx-vp9", "-g", "25", "-b:v", "200k", str(out)],
        check=True,
    )  # fmt: skip
    return out


def _brightness(page: Any) -> float:
    """Mean brightness (0-255) of the video's current frame."""
    return float(
        page.evaluate(
            """() => {
              const v = document.querySelector('.video-frame video');
              const c = document.createElement('canvas'); c.width = 32; c.height = 18;
              const g = c.getContext('2d'); g.drawImage(v, 0, 0, 32, 18);
              const d = g.getImageData(0, 0, 32, 18).data; let s = 0;
              for (let i = 0; i < d.length; i += 4) s += d[i];
              return s / (d.length / 4);
            }"""
        )
    )


def _settle(page: Any) -> None:
    page.wait_for_function(
        "() => { const v = document.querySelector('.video-frame video');"
        " return v && v.readyState >= 2 && !v.seeking; }",
        timeout=60_000,
    )
    page.wait_for_timeout(250)


def test_synced_video_lines_up_with_the_log(server: Any, browser: Any, flash_video: Path) -> None:
    """After a two-point sync, the flash shows exactly when the log says it happened."""
    import httpx

    base, ids = server
    fid = ids["manoeuvres.bin"]
    with flash_video.open("rb") as fh:
        r = httpx.post(f"{base}/api/flights/{fid}/videos", files={"file": ("flashes.webm", fh)})
    assert r.status_code == 201, r.text
    video = r.json()["video"]
    # the user marks the first and last flash in both clocks (what "Add sync point" stores)
    points = [[FLASHES[0], _log_time(FLASHES[0])], [FLASHES[2], _log_time(FLASHES[2])]]
    rate = (points[1][1] - points[0][1]) / (points[1][0] - points[0][0])
    offset = points[0][1] - rate * points[0][0]
    sync = {"offset_s": offset, "rate": rate, "sync_method": "points", "sync_points": points}
    r = httpx.patch(f"{base}/api/videos/{video['id']}", json=sync)
    assert r.status_code == 200, r.text

    # the middle flash, which was not marked: on inside its 0.2 s, off just outside
    mid = _log_time(FLASHES[1])
    for t, lit in ((mid + 0.05, True), (mid + 0.15, True), (mid - 0.1, False), (mid + 0.3, False)):
        page = _open(browser, f"{base}/flights/{fid}/replay?t={t:.3f}")
        _settle(page)
        b = _brightness(page)
        assert (b > 200) if lit else (b < 50), (t - mid, b)
        page.close()

    # playing, the video follows the clock (drift corrected within 100 ms; the readout is 0.1 s)
    page = _open(browser, f"{base}/flights/{fid}/replay?t={mid - 3:.1f}")
    _settle(page)
    page.keyboard.press("Space")
    worst = 0.0
    for _ in range(6):
        page.wait_for_timeout(400)
        v, t = page.evaluate(
            "() => [document.querySelector('.video-frame video').currentTime,"
            " document.querySelector('.time-readout').textContent]"
        )
        mm, rest = t.split("/")[0].strip().split(":")
        log_t = int(mm) * 60 + float(rest)
        worst = max(worst, abs(v - (log_t - offset) / rate))
    page.keyboard.press("Space")
    assert worst < 0.25, worst
    # the timeline shows which part of the flight has video
    assert page.locator(".timeline .video-coverage").count() == 1
    assert "Synced from 2 points" in page.locator(".video-status").inner_text()
    page.close()
