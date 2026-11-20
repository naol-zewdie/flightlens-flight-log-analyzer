"""Threshold profiles: format, learning, engine use, storage, API and CLI."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from flightlens import cli, parsers
from flightlens.api import create_app
from flightlens.demo import seed
from flightlens.engine import Verdict, analyze
from flightlens.profiles import (
    MIN_LEARN_FLIGHTS,
    Profile,
    ProfileError,
    learn_profile,
    load_profile,
    parse_profile,
)
from flightlens.sim.benchmark import Fault, copter
from flightlens.store import Store

QUAD = [f"quad-0{i}-healthy.bin" for i in range(1, 5)]
QUAD_VIB = "quad-10-vibration.bin"


# --- format and validation -------------------------------------------------------------


def test_valid_profile_is_normalised() -> None:
    p = parse_profile({"name": "Quad-7", "checks": {"vibration.level": {"attention": 25}}})
    assert p.overrides == {"vibration.level": {"attention": 25.0}}
    assert p.source == "file"


@pytest.mark.parametrize(
    ("checks", "message"),
    [
        ({"no.such": {"attention": 1}}, "unknown check"),
        ({"vibration.level": {"atention": 1}}, "unknown setting"),
        ({"vibration.level": {"attention": "high"}}, "not a number"),
        ({"vibration.level": {"attention": True}}, "not a number"),
        ({"vibration.level": {"attention": float("nan")}}, "finite"),
        ({"vibration.level": {"attention": 70}}, "must not be above problem"),  # default 60
        ({"battery.sag": {"attention": 3.2}}, "must not be below problem"),  # lower is worse
        ({"vibration.level": {}}, "non-empty"),
    ],
)
def test_bad_profiles_are_refused(checks: dict[str, Any], message: str) -> None:
    with pytest.raises(ProfileError, match=message):
        parse_profile({"name": "x", "checks": checks})


def test_profile_needs_a_name() -> None:
    with pytest.raises(ProfileError, match="name"):
        parse_profile({"checks": {}})


def test_digest_depends_on_values_not_order() -> None:
    a = Profile("a", {"battery.sag": {"attention": 3.6}, "vibration.level": {"attention": 25.0}})
    b = Profile("b", {"vibration.level": {"attention": 25.0}, "battery.sag": {"attention": 3.6}})
    c = Profile("a", {"vibration.level": {"attention": 26.0}})
    assert a.digest == b.digest != c.digest


def test_yaml_round_trip(tmp_path: Path) -> None:
    rows = [_row(i, vibration=15.0 + i * 0.1) for i in range(5)]
    learned = learn_profile("Quad-7", rows)
    path = tmp_path / "quad7.yaml"
    path.write_text(learned.to_yaml(), encoding="utf-8")
    again = load_profile(path)
    assert again.overrides == learned.overrides
    assert again.source == "learned" and again.digest == learned.digest


def test_invalid_yaml_is_a_profile_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("name: [unclosed", encoding="utf-8")
    with pytest.raises(ProfileError, match="not valid YAML"):
        load_profile(path)


# --- learning ------------------------------------------------------------------------


def _row(
    i: int, verdict: str = "healthy", fmt: str = "dataflash", **metrics: float
) -> dict[str, Any]:
    return {"id": f"f{i}", "verdict": verdict, "log_format": fmt, "metrics": metrics}


def test_learning_needs_enough_eligible_flights() -> None:
    rows = [_row(i, vibration=15.0) for i in range(MIN_LEARN_FLIGHTS - 1)]
    rows += [_row(9, "problem", vibration=15.0)]  # a Problem flight never counts
    with pytest.raises(ProfileError, match=f"at least {MIN_LEARN_FLIGHTS}"):
        learn_profile("x", rows)


def test_learned_value_is_median_plus_drift_margin() -> None:
    rows = [_row(i, vibration=v) for i, v in enumerate([15.0, 15.5, 16.0, 16.5, 17.0])]
    p = learn_profile("x", rows)
    # median 16, margin max(30% of 16, 3 m/s², 3 robust sigma) = 4.8
    assert p.overrides == {"vibration.level": {"attention": pytest.approx(20.8)}}
    b = p.basis["values"]["vibration.level.attention"]  # type: ignore[index]
    assert b["flights"] == 5 and b["default"] == 30.0 and not b["clamped"]


def test_learned_values_are_clamped() -> None:
    noisy = [_row(i, vibration=55.0, imbalance=1.0, cell_min=4.1) for i in range(5)]
    p = learn_profile("x", noisy).overrides
    assert p["vibration.level"]["attention"] == pytest.approx(57.0)  # 90% of the way to problem
    assert p["motor.imbalance"]["attention"] == pytest.approx(0.05)  # tightest: half the default
    assert p["battery.sag"]["attention"] == pytest.approx(3.7)  # tightest for lower-is-worse
    assert "problem" not in p["vibration.level"]  # problem thresholds are never learned


def test_px4_flights_learn_the_px4_key() -> None:
    rows = [_row(i, fmt="ulog", vibration=10.0 + i * 0.2) for i in range(5)]
    assert set(learn_profile("x", rows).overrides["vibration.level"]) == {"px4_attention"}


def test_nothing_learnable_is_an_error() -> None:
    with pytest.raises(ProfileError, match="None of the learnable"):
        learn_profile("x", [_row(i, score=100) for i in range(5)])


# --- engine ------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sim_logs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("sim")
    logs = {f"healthy{i}": copter(d / f"healthy{i}.bin", None, i, day=i) for i in range(4)}
    logs["mild"] = copter(d / "mild.bin", Fault("vibration", 60, 140, 26), 7, day=5)
    logs["calm"] = copter(d / "calm.bin", Fault("vibration", 60, 140, 18), 8, day=6)
    return logs


@pytest.fixture(scope="module")
def learned(sim_logs: dict[str, Path]) -> Profile:
    from flightlens.metrics import flight_metrics

    rows = []
    for name, path in sim_logs.items():
        if name.startswith("healthy"):
            flight = parsers.load(path)
            report = analyze(flight)
            rows.append(
                {
                    "id": name,
                    "verdict": report.verdict.value,
                    "log_format": flight.metadata.log_format,
                    "metrics": flight_metrics(flight, report),
                }
            )
    return learn_profile("Sim quad", rows)


def test_learned_profile_changes_a_verdict_only_where_expected(
    sim_logs: dict[str, Path], learned: Profile
) -> None:
    mild = parsers.load(sim_logs["mild"])
    assert analyze(mild).verdict is Verdict.HEALTHY  # 26 m/s² is fine by the defaults
    report = analyze(mild, profile=learned)
    assert report.verdict is Verdict.ATTENTION  # but well above this aircraft's usual 17.5
    assert [f.check_id for f in report.findings if f.severity.value == "attention"] == [
        "vibration.level"
    ]
    for name in ("healthy0", "healthy3", "calm"):
        assert analyze(parsers.load(sim_logs[name]), profile=learned).verdict is Verdict.HEALTHY


def test_report_records_the_profile(sim_logs: dict[str, Path], learned: Profile) -> None:
    flight = parsers.load(sim_logs["healthy0"])
    assert analyze(flight).to_dict()["profile"] is None
    block = analyze(flight, profile=learned).to_dict()["profile"]
    assert block == {
        "name": "Sim quad",
        "source": "learned",
        "digest": learned.digest,
        "overrides": learned.overrides,
    }


def test_explicit_config_wins_over_the_profile(sim_logs: dict[str, Path]) -> None:
    flight = parsers.load(sim_logs["mild"])
    strict = Profile("strict", {"vibration.level": {"attention": 20.0}})
    assert analyze(flight, profile=strict).verdict is Verdict.ATTENTION
    relaxed = analyze(flight, profile=strict, config={"vibration.level": {"attention": 29.0}})
    assert relaxed.verdict is Verdict.HEALTHY


# --- storage ---------------------------------------------------------------------------


def test_store_keeps_profiles_and_assignments(tmp_path: Path) -> None:
    store = Store(tmp_path)
    p = Profile("Quad-7", {"vibration.level": {"attention": 25.0}}, source="user", notes="n")
    store.save_profile(p)
    store.assign_profile("sn:AB", "Quad-7")
    assert store.get_profile("Quad-7") == p
    assert store.profile_assignments() == {"sn:AB": p}
    store.save_profile(Profile("Quad-7", {"vibration.level": {"attention": 24.0}}, source="user"))
    assert store.profile_assignments()["sn:AB"].overrides["vibration.level"]["attention"] == 24.0
    assert store.delete_profile("Quad-7")
    assert store.profile_assignments() == {}  # the aircraft is back on the defaults


# --- API ---------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def quad_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    data = tmp_path_factory.mktemp("quad")
    seed(data, only=[*QUAD, QUAD_VIB])
    return data


@pytest.fixture
def client(quad_dir: Path) -> Iterator[TestClient]:
    with TestClient(create_app(quad_dir), base_url="http://localhost") as c:
        yield c


def _wait(client: TestClient, timeout: float = 180) -> list[dict[str, Any]]:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        flights: list[dict[str, Any]] = client.get("/api/flights").json()["flights"]
        if all(f["status"] in ("done", "failed") for f in flights):
            return flights
        time.sleep(0.5)
    raise AssertionError("re-analysis did not finish")


def test_learn_assign_and_reanalyze(client: TestClient) -> None:
    (quad,) = client.get("/api/aircraft").json()["aircraft"]
    key = quad["key"]
    r = client.post(f"/api/aircraft/{key}/profile/learn", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["queued"] == 5 and body["profile"]["source"] == "learned"
    assert body["profile"]["defaults"]["vibration.level"] == {"attention": 30.0}
    assert body["profile"]["name"].startswith("Learned: CubeOrange")

    # meanwhile the aircraft keeps its flights (with their earlier results)
    detail = client.get(f"/api/aircraft/{key}").json()
    assert detail["flights"] == 5 and detail["profile"]["name"] == body["profile"]["name"]

    flights = _wait(client)
    assert {f["status"] for f in flights} == {"done"}
    report = client.get(f"/api/flights/{flights[0]['id']}/report.json").json()
    assert report["profile"]["digest"] == body["profile"]["digest"]

    # back to the defaults: re-analyzed without a profile
    r = client.put(f"/api/aircraft/{key}/profile", json={"profile": None})
    assert r.json()["queued"] == 5
    flights = _wait(client)
    assert client.get(f"/api/flights/{flights[0]['id']}/report.json").json()["profile"] is None
    assert client.get(f"/api/aircraft/{key}").json()["pending"] == 0


def test_profile_crud_and_errors(client: TestClient) -> None:
    bad = client.put("/api/profiles/Heavy", json={"checks": {"vibration.level": {"x": 1}}})
    assert bad.status_code == 422 and "unknown setting" in bad.json()["detail"]
    ok = client.put("/api/profiles/Heavy", json={"checks": {"vibration.level": {"attention": 35}}})
    assert ok.status_code == 200 and ok.json()["profile"]["source"] == "user"
    assert "Heavy" in [p["name"] for p in client.get("/api/profiles").json()["profiles"]]
    assert client.put("/api/aircraft/sn:nope/profile", json={"profile": "Heavy"}).status_code == 404
    (quad,) = client.get("/api/aircraft").json()["aircraft"]
    assert (
        client.put(f"/api/aircraft/{quad['key']}/profile", json={"profile": "Nope"}).status_code
        == 404
    )
    assert client.delete("/api/profiles/Heavy").status_code == 200
    assert client.get("/api/profiles/Heavy").status_code == 404


def test_learning_with_too_few_flights_is_409(tmp_path: Path) -> None:
    seed(tmp_path, only=QUAD[:2])
    with TestClient(create_app(tmp_path), base_url="http://localhost") as c:
        (quad,) = c.get("/api/aircraft").json()["aircraft"]
        r = c.post(f"/api/aircraft/{quad['key']}/profile/learn", json={})
        assert r.status_code == 409 and "at least" in r.json()["detail"]


def test_read_only_refuses_profile_changes(quad_dir: Path) -> None:
    with TestClient(create_app(quad_dir, read_only=True), base_url="http://localhost") as c:
        assert c.put("/api/profiles/X", json={"checks": {}}).status_code == 403
        assert c.get("/api/metric-specs").status_code == 200


# --- CLI ---------------------------------------------------------------------------------

runner = CliRunner()


def test_cli_learn_then_analyze_with_profile(sim_logs: dict[str, Path], tmp_path: Path) -> None:
    folder = tmp_path / "quad"
    folder.mkdir()
    for name, path in sim_logs.items():
        if name.startswith("healthy"):
            (folder / path.name).write_bytes(path.read_bytes())
    out = tmp_path / "quad.yaml"
    r = runner.invoke(cli.app, ["profile", "learn", str(folder), "--name", "Q", "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert yaml.safe_load(out.read_text(encoding="utf-8"))["learned"] is True

    mild = str(sim_logs["mild"])
    assert runner.invoke(cli.app, ["analyze", mild]).exit_code == 0
    r = runner.invoke(cli.app, ["analyze", mild, "--profile", str(out)])
    assert r.exit_code == 1 and "profile Q" in r.output


def test_cli_bad_profile_is_a_usage_error(sim_logs: dict[str, Path], tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: x\nchecks:\n  vibration.level: {attention: 99}\n", encoding="utf-8")
    r = runner.invoke(cli.app, ["analyze", str(sim_logs["calm"]), "--profile", str(bad)])
    assert r.exit_code == cli.EXIT_USAGE
