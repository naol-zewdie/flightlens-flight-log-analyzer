"""Release hygiene: versions agree, legal files ship, security headers are sent."""

from __future__ import annotations

import base64
import hashlib
import logging
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from flightlens import __version__, cli
from flightlens.api import create_app, spa_csp

ROOT = Path(__file__).parent.parent


def test_version_and_changelog_agree() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == __version__
    assert f"## [{__version__}]" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    for name in ("LICENSE", "SECURITY.md", "CONTRIBUTING.md"):
        assert (ROOT / name).is_file(), name


def test_security_headers_and_page_csp(tmp_path: Path) -> None:
    web = tmp_path / "web"
    web.mkdir()
    inline = "document.documentElement.dataset.theme = 'dark';"
    (web / "index.html").write_text(f"<html><script>{inline}</script></html>", encoding="utf-8")
    with TestClient(create_app(tmp_path / "data", web), base_url="http://localhost") as c:
        for path in ("/api/flights", "/api/nope", "/flights/abc"):
            h = c.get(path).headers
            assert h["x-content-type-options"] == "nosniff", path
            assert h["referrer-policy"] == "no-referrer" and h["x-frame-options"] == "DENY"
        csp = c.get("/flights/abc").headers["content-security-policy"]
    digest = base64.b64encode(hashlib.sha256(inline.encode()).digest()).decode()
    assert f"'sha256-{digest}'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-eval" not in csp and "script-src 'self'" in csp
    assert spa_csp("<html></html>").startswith("default-src 'self'; script-src 'self';")


def test_serve_warns_when_reachable_beyond_this_computer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)  # do not actually serve
    monkeypatch.setattr("flightlens.ops.setup_logging", lambda *a, **k: None)
    with caplog.at_level(logging.WARNING, logger="flightlens.server"):
        r = CliRunner().invoke(cli.app, ["serve", "--data", str(tmp_path), "--host", "0.0.0.0"])
    assert r.exit_code == 0, r.output
    assert any("no login" in m for m in caplog.messages)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="flightlens.server"):
        CliRunner().invoke(cli.app, ["serve", "--data", str(tmp_path)])
    assert not any("no login" in m for m in caplog.messages)
