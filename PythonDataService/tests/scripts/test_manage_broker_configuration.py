"""The cutover CLI: plan to a file, quote the token, apply.

Every test chdirs into ``tmp_path`` so the ``.env`` these commands read is the
one the test wrote, never a developer's real ``PythonDataService/.env``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.broker_configuration.store import profiles_database_path
from scripts import manage_broker_configuration as cli

LEGACY_LIVE_ENV = {
    "ALPACA_MODE": "live",
    "ALPACA_LIVE_LOSS_FRACTION": "0.05",
    "ALPACA_LIVE_LOSS_USD": "5000",
    "ALPACA_LIVE_SHADOW_SESSIONS": "3",
    "ALPACA_LIVE_ARMING_MAX_SESSIONS": "20",
    "ALPACA_LIVE_XH_ENTRY_BPS": "11",
    "ALPACA_LIVE_XH_EXIT_BPS": "17.5",
}


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def legacy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in LEGACY_LIVE_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ALPACA_API_KEY_ID", "key-id")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "secret-key")


def _plan_file(tmp_path: Path) -> Path:
    return tmp_path / "broker-config-import.json"


def _run_plan(tmp_path: Path, *extra: str) -> tuple[int, dict]:
    code = cli.main(
        [
            "--clerk-dir",
            str(tmp_path / "clerk"),
            "plan",
            "--plan-out",
            str(_plan_file(tmp_path)),
            *extra,
        ]
    )
    payload = json.loads(_plan_file(tmp_path).read_text(encoding="utf-8"))
    return code, payload


@pytest.mark.usefixtures("legacy_environment")
def test_plan_writes_a_file_without_creating_the_profiles_database(tmp_path: Path) -> None:
    """The read-only half of the ceremony, asserted on the filesystem.

    ``ProfilesStore.open`` creates and migrates, so the CLI checks the database
    exists before opening it. Without that check a preview would leave a
    profiles database behind on an installation that has not cut over.
    """
    code, payload = _run_plan(tmp_path)

    assert code == 0
    assert not profiles_database_path(tmp_path / "clerk").exists()
    assert payload["endpoint_mode"] == "live"
    assert payload["live_envelope"]["loss_usd"] == 5000.0


@pytest.mark.usefixtures("legacy_environment")
def test_a_plan_survives_the_round_trip_through_its_file(tmp_path: Path) -> None:
    """The token verifies after JSON, which is where int/float fidelity could die.

    ``json`` writes ``5000.0`` with its decimal point and ``3`` without one, so a
    float stays a float and a session count stays an ``int``. If that stopped
    holding, the reconstructed plan would hash differently and the operator's
    token would stop matching — which is the failure mode this asserts against.
    """
    _run_plan(tmp_path)
    payload = json.loads(_plan_file(tmp_path).read_text(encoding="utf-8"))

    rebuilt = cli._read_plan(_plan_file(tmp_path))

    assert rebuilt.confirmation_token == payload["confirmation_token"]
    assert rebuilt.live_envelope is not None
    assert type(rebuilt.live_envelope.loss_usd) is float
    assert type(rebuilt.live_envelope.shadow_sessions) is int
    assert rebuilt.envelope_sha == payload["envelope_sha"]


@pytest.mark.usefixtures("legacy_environment")
def test_plan_then_apply_creates_and_stages_the_profile(tmp_path: Path) -> None:
    _, payload = _run_plan(tmp_path)

    code = cli.main(
        [
            "--clerk-dir",
            str(tmp_path / "clerk"),
            "apply",
            "--plan-file",
            str(_plan_file(tmp_path)),
            "--confirmation-token",
            payload["confirmation_token"],
        ]
    )

    assert code == 0
    assert profiles_database_path(tmp_path / "clerk").exists()


@pytest.mark.usefixtures("legacy_environment")
def test_applying_twice_is_idempotent(tmp_path: Path) -> None:
    """A second plan/apply cycle adopts the existing profile rather than duplicating."""
    _, first = _run_plan(tmp_path)
    cli.main(
        [
            "--clerk-dir",
            str(tmp_path / "clerk"),
            "apply",
            "--plan-file",
            str(_plan_file(tmp_path)),
            "--confirmation-token",
            first["confirmation_token"],
        ]
    )

    _, second = _run_plan(tmp_path)
    code = cli.main(
        [
            "--clerk-dir",
            str(tmp_path / "clerk"),
            "apply",
            "--plan-file",
            str(_plan_file(tmp_path)),
            "--confirmation-token",
            second["confirmation_token"],
        ]
    )

    assert code == 0
    assert second["already_imported"] is not None


@pytest.mark.usefixtures("legacy_environment")
def test_a_wrong_token_exits_two(tmp_path: Path) -> None:
    """Exit 2 is "the ceremony refused", distinct from 1 "could not run as asked"."""
    _run_plan(tmp_path)

    code = cli.main(
        [
            "--clerk-dir",
            str(tmp_path / "clerk"),
            "apply",
            "--plan-file",
            str(_plan_file(tmp_path)),
            "--confirmation-token",
            "0" * 64,
        ]
    )

    assert code == 2


def test_an_empty_environment_refuses_rather_than_importing_nothing(tmp_path: Path) -> None:
    code = cli.main(
        [
            "--clerk-dir",
            str(tmp_path / "clerk"),
            "plan",
            "--plan-out",
            str(_plan_file(tmp_path)),
        ]
    )

    assert code == 2
    assert not _plan_file(tmp_path).exists()


@pytest.mark.usefixtures("legacy_environment")
def test_a_plan_file_with_an_unexpected_key_is_refused(tmp_path: Path) -> None:
    """Exact key-set equality: an extra key means this is not the hashed document."""
    _run_plan(tmp_path)
    payload = json.loads(_plan_file(tmp_path).read_text(encoding="utf-8"))
    payload["smuggled"] = "value"
    _plan_file(tmp_path).write_text(json.dumps(payload), encoding="utf-8")

    code = cli.main(
        [
            "--clerk-dir",
            str(tmp_path / "clerk"),
            "apply",
            "--plan-file",
            str(_plan_file(tmp_path)),
            "--confirmation-token",
            payload["confirmation_token"],
        ]
    )

    assert code == 2


@pytest.mark.usefixtures("legacy_environment")
def test_the_plan_reports_the_next_steps_beside_the_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Next steps are printed, never written into the plan the token attests to."""
    _run_plan(tmp_path)

    printed = json.loads(capsys.readouterr().out)
    stored = json.loads(_plan_file(tmp_path).read_text(encoding="utf-8"))

    assert "next_steps" not in stored
    assert any("Delete" in step for step in printed["next_steps"])
    assert any("ALPACA_LIVE_LOSS_USD" in step for step in printed["next_steps"])
