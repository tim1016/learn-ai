"""The arming operator CLI: status, plan, apply, disarm (ADR 0059 D3, design R13)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.utils.session_anchors import MAX_TIMESTAMP_MS
from scripts.manage_alpaca_arming import main
from tests.broker.alpaca.clerk.live_arming_fixtures import (
    ARMED_AT_MS,
    ARMING_SID,
    activate_shadow_fence,
    arming_ready,
    live_settings,
    record_sealed_binding,
)
from tests.broker.alpaca.clerk.live_envelope_fixtures import LIVE_ACCT

SETTINGS = live_settings()


@pytest.fixture()
def roots(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "clerk", tmp_path / "runner"


def _flags(roots: tuple[Path, Path]) -> list[str]:
    artifacts_root, live_state_root = roots
    return ["--artifacts-root", str(artifacts_root), "--live-state-root", str(live_state_root)]


def _last_object(capsys: pytest.CaptureFixture[str]) -> dict:
    return json.loads(capsys.readouterr().out.splitlines()[-1])


def _arm(roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str], *, now_ms: int = ARMED_AT_MS) -> dict:
    """Run the real plan → apply pair through the CLI and return the record object."""
    artifacts_root, _live_state_root = roots
    plan_file = artifacts_root / "plan.json"
    assert (
        main(
            [
                *_flags(roots),
                "plan",
                "--strategy-instance-id",
                ARMING_SID,
                "--plan-out",
                str(plan_file),
                "--now-ms",
                str(now_ms),
            ],
            settings=SETTINGS,
        )
        == 0
    )
    plan = _last_object(capsys)
    assert (
        main(
            [
                *_flags(roots),
                "apply",
                "--plan-file",
                str(plan_file),
                "--confirmation-token",
                plan["confirmation_token"],
                "--now-ms",
                str(now_ms),
            ],
            settings=SETTINGS,
        )
        == 0
    )
    return _last_object(capsys)


def test_status_on_an_account_with_no_records_answers_unarmed(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, _live_state_root = roots
    activate_shadow_fence(artifacts_root)

    assert main([*_flags(roots), "status", "--now-ms", str(ARMED_AT_MS)], settings=SETTINGS) == 0

    report = _last_object(capsys)
    assert report["live_account_id"] == LIVE_ACCT
    assert report["armed_instance_count"] == 0
    assert report["envelope_state"] == "configured_unsealed"
    assert report["instances"] == []
    assert report["submission_admitted"] is False
    assert "Slice 7" in report["note"]


def test_plan_writes_only_the_plan_file_and_apply_writes_the_sealed_record(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)

    record = _arm(roots, capsys)

    assert record["kind"] == "armed"
    assert record["live_account_id"] == LIVE_ACCT
    assert record["armed_at_ms"] == ARMED_AT_MS
    assert record["submission_admitted"] is False
    ledger = LiveArmingLedger(artifacts_root, live_account_id=LIVE_ACCT)
    assert len(ledger.records()) == 1
    assert (artifacts_root / "plan.json").is_file()


def test_status_after_arming_counts_the_instance_and_reports_the_sealed_envelope(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    _arm(roots, capsys)

    assert main([*_flags(roots), "status", "--now-ms", str(ARMED_AT_MS)], settings=SETTINGS) == 0

    report = _last_object(capsys)
    assert report["armed_instance_count"] == 1
    assert report["envelope_state"] == "sealed"
    (instance,) = report["instances"]
    assert instance["strategy_instance_id"] == ARMING_SID
    assert instance["state"] == "armed"
    assert instance["reason_code"] is None
    assert (instance["sessions_used"], instance["sessions_remaining"]) == (1, 19)


def test_a_quoted_token_that_is_not_the_plans_refuses_at_exit_two(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    plan_file = artifacts_root / "plan.json"
    assert (
        main(
            [*_flags(roots), "plan", "--strategy-instance-id", ARMING_SID, "--plan-out", str(plan_file),
             "--now-ms", str(ARMED_AT_MS)],
            settings=SETTINGS,
        )
        == 0
    )

    assert (
        main(
            [*_flags(roots), "apply", "--plan-file", str(plan_file), "--confirmation-token", "0" * 64,
             "--now-ms", str(ARMED_AT_MS)],
            settings=SETTINGS,
        )
        == 2
    )

    assert _last_object(capsys)["error"] == "LIVE_ARMING_TOKEN_INVALID"
    assert LiveArmingLedger(artifacts_root, live_account_id=LIVE_ACCT).records() == ()


def test_applying_after_the_confirmation_window_refuses_at_exit_two(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    plan_file = artifacts_root / "plan.json"
    assert (
        main(
            [*_flags(roots), "plan", "--strategy-instance-id", ARMING_SID, "--plan-out", str(plan_file),
             "--confirmation-ttl-ms", "10", "--now-ms", str(ARMED_AT_MS)],
            settings=SETTINGS,
        )
        == 0
    )
    token = _last_object(capsys)["confirmation_token"]

    assert (
        main(
            [*_flags(roots), "apply", "--plan-file", str(plan_file), "--confirmation-token", token,
             "--now-ms", str(ARMED_AT_MS + 11)],
            settings=SETTINGS,
        )
        == 2
    )

    assert _last_object(capsys)["error"] == "LIVE_ARMING_PLAN_EXPIRED"


def test_planning_without_a_current_shadow_receipt_refuses_by_the_adrs_code(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, live_state_root = roots
    activate_shadow_fence(artifacts_root)
    record_sealed_binding(live_state_root)

    assert (
        main(
            [*_flags(roots), "plan", "--strategy-instance-id", ARMING_SID, "--now-ms", str(ARMED_AT_MS)],
            settings=SETTINGS,
        )
        == 2
    )

    report = _last_object(capsys)
    assert report["error"] == "LIVE_SHADOW_INCOMPLETE"
    assert report["submission_admitted"] is False


def test_disarm_revokes_and_status_names_the_revocation(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    armed = _arm(roots, capsys)

    assert (
        main(
            [*_flags(roots), "disarm", "--strategy-instance-id", ARMING_SID, "--now-ms", str(ARMED_AT_MS + 1)],
            settings=SETTINGS,
        )
        == 0
    )
    revocation = _last_object(capsys)
    assert revocation["kind"] == "disarmed"
    assert revocation["revokes_record_sha256"] == armed["record_sha256"]

    assert main([*_flags(roots), "status", "--now-ms", str(ARMED_AT_MS + 1)], settings=SETTINGS) == 0
    report = _last_object(capsys)
    assert report["armed_instance_count"] == 0
    (instance,) = report["instances"]
    assert (instance["state"], instance["reason_code"]) == ("disarmed", "LIVE_ARMING_REVOKED")


def test_disarming_something_that_was_never_armed_refuses_at_exit_two(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)

    assert (
        main(
            [*_flags(roots), "disarm", "--strategy-instance-id", ARMING_SID, "--now-ms", str(ARMED_AT_MS)],
            settings=SETTINGS,
        )
        == 2
    )

    assert _last_object(capsys)["error"] == "LIVE_ARMING_NOT_ARMED"


def test_a_plan_file_that_is_not_a_plan_is_an_operator_error_at_exit_one(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    junk = artifacts_root / "junk.json"
    junk.write_text('{"schema_version": 1}', encoding="utf-8")

    assert (
        main(
            [*_flags(roots), "apply", "--plan-file", str(junk), "--confirmation-token", "0" * 64],
            settings=SETTINGS,
        )
        == 1
    )
    assert "not an arming plan" in _last_object(capsys)["error"]

    absent = artifacts_root / "nope.json"
    assert (
        main(
            [*_flags(roots), "apply", "--plan-file", str(absent), "--confirmation-token", "0" * 64],
            settings=SETTINGS,
        )
        == 1
    )
    assert "unreadable" in _last_object(capsys)["error"]


@pytest.mark.parametrize(
    ("argv", "fragment"),
    [
        pytest.param([], "operation", id="missing-subcommand"),
        pytest.param(["nosuch"], "invalid choice", id="unknown-subcommand"),
        pytest.param(["plan"], "--strategy-instance-id", id="missing-required-flag"),
        pytest.param(
            ["plan", "--strategy-instance-id", "s", "--confirmation-ttl-ms", "0"],
            "--confirmation-ttl-ms",
            id="ttl-below-bound",
        ),
        pytest.param(
            ["plan", "--strategy-instance-id", "s", "--confirmation-ttl-ms", "300001"],
            "--confirmation-ttl-ms",
            id="ttl-above-bound",
        ),
        pytest.param(
            ["status", "--now-ms", str(MAX_TIMESTAMP_MS + 1)], "--now-ms", id="instant-outside-the-domain"
        ),
        pytest.param(["disarm", "--strategy-instance-id", "s", "--nope"], "unrecognized arguments", id="unknown-flag"),
    ],
)
def test_every_usage_refusal_is_one_json_object_at_exit_one(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str], argv: list[str], fragment: str
) -> None:
    """Exit ``1``, never argparse's bare ``2`` -- which "the ceremony refused" owns.

    A script reading only the exit code could not otherwise tell a typo from a
    real-money arming precondition that has not been met.
    """
    assert main([*_flags(roots), *argv], settings=SETTINGS) == 1

    assert fragment in _last_object(capsys)["error"]


def test_a_ledger_row_that_will_not_verify_is_an_evidence_error_at_exit_one(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    _arm(roots, capsys)
    ledger = LiveArmingLedger(artifacts_root, live_account_id=LIVE_ACCT)
    ledger.path.write_text(
        ledger.path.read_text(encoding="utf-8").replace('"max_sessions":20', '"max_sessions":90'),
        encoding="utf-8",
    )

    assert main([*_flags(roots), "status", "--now-ms", str(ARMED_AT_MS)], settings=SETTINGS) == 1

    assert "digest does not verify" in _last_object(capsys)["error"]
