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


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with pw.sync_playwright() as p:
        last: Exception | None = None
        for kwargs in ({}, {"channel": "msedge"}, {"channel": "chrome"}):
            try:
                b = p.chromium.launch(**kwargs)
                break
            except Exception as exc:  # browser not installed
                last = exc
        else:
            pytest.skip(f"no browser for Playwright: {last}")
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
    assert 50 + 1.2 < t1 <= 50 + elapsed + 0.3, (t1, elapsed)
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
