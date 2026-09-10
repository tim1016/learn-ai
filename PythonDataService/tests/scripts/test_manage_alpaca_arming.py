"""The arming operator CLI: status, plan, apply, disarm (ADR 0059 D3, design R13)."""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.live_arming import (
    LIVE_ARMING_INSTANCE_UNSEALED,
    LIVE_ARMING_SEAL_CHANGED,
)
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.broker.alpaca.clerk.shadow_activation import ShadowActivationStore
from app.broker.alpaca.clerk.sqlite.activation import ACTIVATION_FILENAME, ActivationStore
from app.broker.alpaca.config import reset_alpaca_settings_for_testing
from app.utils.session_anchors import MAX_TIMESTAMP_MS
from scripts.manage_alpaca_arming import _SUBMISSION_ADMITTED_NOTE, main
from tests.broker.alpaca.clerk.live_arming_fixtures import (
    ARMED_AT_MS,
    ARMING_SID,
    activate_shadow_fence,
    arming_ready,
    live_settings,
    record_sealed_binding,
)
from tests.broker.alpaca.clerk.live_authority_fixtures import live_activation
from tests.broker.alpaca.clerk.live_envelope_fixtures import LIVE_ACCT

SETTINGS = live_settings()


@pytest.fixture()
def roots(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "clerk", tmp_path / "runner"


@pytest.fixture()
def incomplete_live_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    """``ALPACA_MODE=live`` with no envelope values -- a half-edited live ``.env``.

    ``AlpacaSettings`` refuses to construct in this state, so every test using
    this fixture calls ``main`` with no injected ``settings`` and exercises the
    real ``get_alpaca_settings()`` boundary the CLI's contract depends on.
    """
    # ``AlpacaSettings`` reads ``.env`` from the process working directory.
    # A developer's complete local envelope must not fill the deliberate gaps
    # below, so run this boundary test from its own empty directory.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALPACA_API_KEY_ID", "key")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "secret")
    monkeypatch.setenv("ALPACA_MODE", "live")
    for suffix in (
        "LOSS_FRACTION",
        "LOSS_USD",
        "SHADOW_SESSIONS",
        "ARMING_MAX_SESSIONS",
        "XH_ENTRY_BPS",
        "XH_EXIT_BPS",
    ):
        monkeypatch.delenv(f"ALPACA_LIVE_{suffix}", raising=False)
    reset_alpaca_settings_for_testing()
    yield
    reset_alpaca_settings_for_testing()


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


def _graduate_live_account(artifacts_root: Path) -> None:
    from tests.broker.alpaca.clerk.sqlite.test_cutover_live import (
        test_a_never_legacy_live_account_graduates_end_to_end,
    )

    test_a_never_legacy_live_account_graduates_end_to_end(artifacts_root)


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
    assert "no live authority is activated for this account" in report["note"]


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


def test_status_lists_an_armed_graduated_account_without_a_shadow_fence(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, live_state_root = roots
    _graduate_live_account(artifacts_root)
    record_sealed_binding(
        live_state_root,
        strategy_instance_id=ARMING_SID,
        sealed_account_id=LIVE_ACCT,
    )
    _arm(roots, capsys)

    assert main([*_flags(roots), "status", "--now-ms", str(ARMED_AT_MS)], settings=SETTINGS) == 0

    report = _last_object(capsys)
    assert report["live_account_id"] == LIVE_ACCT
    assert report["armed_instance_count"] == 1
    assert report["instances"][0]["strategy_instance_id"] == ARMING_SID


def test_status_refuses_to_choose_between_two_directly_activated_live_accounts(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, _live_state_root = roots
    store = ActivationStore(artifacts_root / "accounts" / "alpaca")
    store.append(live_activation(account_id=LIVE_ACCT))
    store.append(live_activation(account_id="9LIVE0002"))

    assert main([*_flags(roots), "status", "--now-ms", str(ARMED_AT_MS)], settings=SETTINGS) == 2

    refusal = _last_object(capsys)
    assert refusal["error"] == LIVE_ARMING_INSTANCE_UNSEALED
    assert "more than one account" in refusal["detail"]


def test_status_refuses_different_accounts_across_shadow_and_live_activations(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, _live_state_root = roots
    activate_shadow_fence(artifacts_root, live_account_id=LIVE_ACCT)
    ActivationStore(artifacts_root / "accounts" / "alpaca").append(
        live_activation(account_id="9LIVE0002")
    )

    assert main([*_flags(roots), "status", "--now-ms", str(ARMED_AT_MS)], settings=SETTINGS) == 2

    refusal = _last_object(capsys)
    assert refusal["error"] == LIVE_ARMING_INSTANCE_UNSEALED
    assert LIVE_ACCT in refusal["detail"]
    assert "9LIVE0002" in refusal["detail"]


def test_list_status_refuses_an_invalid_live_activation_ledger_as_one_json_object(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, _live_state_root = roots
    ledger_path = artifacts_root / "accounts" / "alpaca" / ACTIVATION_FILENAME
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text("not-json\n", encoding="utf-8")

    assert main([*_flags(roots), "status", "--now-ms", str(ARMED_AT_MS)], settings=SETTINGS) == 2

    refusal = _last_object(capsys)
    assert refusal["error"] == LIVE_ARMING_INSTANCE_UNSEALED
    assert "Live activation ledger cannot be verified" in refusal["detail"]


def test_list_status_refuses_a_wrong_typed_activation_account_as_one_json_object(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, _live_state_root = roots
    payload = {**asdict(live_activation()), "account_id": None}
    ledger_path = artifacts_root / "accounts" / "alpaca" / ACTIVATION_FILENAME
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    assert main([*_flags(roots), "status", "--now-ms", str(ARMED_AT_MS)], settings=SETTINGS) == 2

    refusal = _last_object(capsys)
    assert refusal["error"] == LIVE_ARMING_INSTANCE_UNSEALED
    assert "activation account_id must be a string" in refusal["detail"]


def test_list_status_refuses_a_graduated_account_with_a_missing_database(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, _live_state_root = roots
    _graduate_live_account(artifacts_root)
    (artifacts_root / "accounts" / "alpaca" / LIVE_ACCT / "clerk.db").unlink()

    assert main([*_flags(roots), "status", "--now-ms", str(ARMED_AT_MS)], settings=SETTINGS) == 2

    refusal = _last_object(capsys)
    assert refusal["error"] == LIVE_ARMING_INSTANCE_UNSEALED
    assert "Live activation is not verified" in refusal["detail"]


def test_list_status_refuses_a_graduated_account_with_a_malformed_database(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, _live_state_root = roots
    _graduate_live_account(artifacts_root)
    database = artifacts_root / "accounts" / "alpaca" / LIVE_ACCT / "clerk.db"
    with sqlite3.connect(database) as conn:
        conn.execute(
            "UPDATE control_meta SET schema_version = 'not-a-version' WHERE id = 1"
        )

    assert main([*_flags(roots), "status", "--now-ms", str(ARMED_AT_MS)], settings=SETTINGS) == 2

    refusal = _last_object(capsys)
    assert refusal["error"] == LIVE_ARMING_INSTANCE_UNSEALED
    assert "Live activation is not verified" in refusal["detail"]


def test_list_status_refuses_an_infinite_database_control_value(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, _live_state_root = roots
    _graduate_live_account(artifacts_root)
    database = artifacts_root / "accounts" / "alpaca" / LIVE_ACCT / "clerk.db"
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE control_meta SET control_revision = 1e999 WHERE id = 1")

    assert main([*_flags(roots), "status", "--now-ms", str(ARMED_AT_MS)], settings=SETTINGS) == 2

    refusal = _last_object(capsys)
    assert refusal["error"] == LIVE_ARMING_INSTANCE_UNSEALED
    assert "Live activation is not verified" in refusal["detail"]


def test_list_status_translates_an_escaped_database_symlink_to_a_typed_refusal(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, _live_state_root = roots
    _graduate_live_account(artifacts_root)
    database = artifacts_root / "accounts" / "alpaca" / LIVE_ACCT / "clerk.db"
    escaped = artifacts_root.parent / "escaped.db"
    database.replace(escaped)
    database.symlink_to(escaped)

    assert main([*_flags(roots), "status", "--now-ms", str(ARMED_AT_MS)], settings=SETTINGS) == 2

    refusal = _last_object(capsys)
    assert refusal["error"] == LIVE_ARMING_INSTANCE_UNSEALED
    assert "path escapes root" in refusal["detail"]


def test_status_uses_the_instances_own_ledger_after_its_binding_is_lost(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, live_state_root = roots
    _graduate_live_account(artifacts_root)
    record_sealed_binding(
        live_state_root,
        strategy_instance_id=ARMING_SID,
        sealed_account_id=LIVE_ACCT,
    )
    _arm(roots, capsys)
    shutil.rmtree(live_state_root / "live_state" / ARMING_SID)

    assert (
        main(
            [*_flags(roots), "status", "--strategy-instance-id", ARMING_SID,
             "--now-ms", str(ARMED_AT_MS)],
            settings=SETTINGS,
        )
        == 0
    )

    report = _last_object(capsys)
    assert report["live_account_id"] == LIVE_ACCT
    assert report["armed_instance_count"] == 0
    assert report["instances"][0]["reason_code"] == LIVE_ARMING_SEAL_CHANGED


def test_status_uses_the_instances_ledger_after_its_shadow_fence_is_lost(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    _arm(roots, capsys)
    ShadowActivationStore(artifacts_root).path.unlink()

    assert (
        main(
            [*_flags(roots), "status", "--strategy-instance-id", ARMING_SID,
             "--now-ms", str(ARMED_AT_MS)],
            settings=SETTINGS,
        )
        == 0
    )

    report = _last_object(capsys)
    assert report["live_account_id"] == LIVE_ACCT
    assert report["armed_instance_count"] == 1
    assert report["instances"][0]["state"] == "armed"


def test_recovered_seal_loss_reports_submission_closed_when_live_authority_is_damaged(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, live_state_root = roots
    _graduate_live_account(artifacts_root)
    record_sealed_binding(
        live_state_root,
        strategy_instance_id=ARMING_SID,
        sealed_account_id=LIVE_ACCT,
    )
    _arm(roots, capsys)
    shutil.rmtree(live_state_root / "live_state" / ARMING_SID)
    (artifacts_root / "accounts" / "alpaca" / LIVE_ACCT / "clerk.db").unlink()

    assert (
        main(
            [*_flags(roots), "status", "--strategy-instance-id", ARMING_SID,
             "--now-ms", str(ARMED_AT_MS)],
            settings=SETTINGS,
        )
        == 0
    )

    report = _last_object(capsys)
    assert report["instances"][0]["reason_code"] == LIVE_ARMING_SEAL_CHANGED
    assert report["submission_admitted"] is False
    assert "Clerk database" in report["note"]


def test_named_status_translates_an_escaped_arming_root_to_a_typed_refusal(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, _live_state_root = roots
    escaped = artifacts_root.parent / "escaped-arming"
    escaped.mkdir()
    accounts = artifacts_root / "accounts"
    accounts.mkdir(parents=True)
    (accounts / "arming").symlink_to(escaped, target_is_directory=True)

    assert (
        main(
            [*_flags(roots), "status", "--strategy-instance-id", ARMING_SID,
             "--now-ms", str(ARMED_AT_MS)],
            settings=SETTINGS,
        )
        == 2
    )

    refusal = _last_object(capsys)
    assert refusal["error"] == LIVE_ARMING_INSTANCE_UNSEALED
    assert "arming ledger cannot be discovered safely" in refusal["detail"]


@pytest.mark.parametrize("operation", ["plan", "status"])
def test_live_binding_with_a_missing_database_is_one_typed_refusal(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str], operation: str
) -> None:
    artifacts_root, live_state_root = roots
    _graduate_live_account(artifacts_root)
    record_sealed_binding(
        live_state_root,
        strategy_instance_id=ARMING_SID,
        sealed_account_id=LIVE_ACCT,
    )
    (artifacts_root / "accounts" / "alpaca" / LIVE_ACCT / "clerk.db").unlink()

    assert (
        main(
            [*_flags(roots), operation, "--strategy-instance-id", ARMING_SID,
             "--now-ms", str(ARMED_AT_MS)],
            settings=SETTINGS,
        )
        == 2
    )

    refusal = _last_object(capsys)
    assert refusal["error"] == LIVE_ARMING_INSTANCE_UNSEALED
    assert "not sealed to a verified Live account" in refusal["detail"]


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


def test_planning_without_a_current_shadow_receipt_plans_with_a_null_receipt(
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
        == 0
    )

    report = _last_object(capsys)
    assert report["shadow_receipt_sha256"] is None
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


def test_disarm_still_runs_when_the_live_environment_will_not_load(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str], incomplete_live_environment: None
) -> None:
    """R4's closed direction survives the configuration it exists to revoke.

    ``main`` is called with no ``settings=``: the arming happened while the
    environment was whole, and the operator now has to revoke it with an
    ``.env`` that no longer loads.
    """
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    armed = _arm(roots, capsys)

    assert (
        main([*_flags(roots), "disarm", "--strategy-instance-id", ARMING_SID,
              "--now-ms", str(ARMED_AT_MS + 1)])
        == 0
    )

    revocation = _last_object(capsys)
    assert revocation["kind"] == "disarmed"
    assert revocation["revokes_record_sha256"] == armed["record_sha256"]


def test_an_unloadable_live_environment_is_one_json_object_at_exit_two(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str], incomplete_live_environment: None
) -> None:
    """The commands that genuinely need settings refuse by name, never by traceback."""
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)

    assert main([*_flags(roots), "plan", "--strategy-instance-id", ARMING_SID,
                 "--now-ms", str(ARMED_AT_MS)]) == 2
    plan_refusal = _last_object(capsys)
    assert plan_refusal["error"] == "LIVE_ENVELOPE_MISSING"
    assert "ALPACA_MODE=live requires" in plan_refusal["detail"]
    assert plan_refusal["submission_admitted"] is False

    assert main([*_flags(roots), "status", "--now-ms", str(ARMED_AT_MS)]) == 2
    assert _last_object(capsys)["error"] == "LIVE_ENVELOPE_MISSING"


def test_a_tampered_plan_file_with_its_original_token_refuses_at_exit_two(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """The plan's ids are its own content hash, so editing the file breaks them.

    Quoting the token the ceremony issued is not enough: the token answers a
    question about the document the plan *was*, and this is a different one.
    """
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
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    token = plan["confirmation_token"]
    plan_file.write_text(json.dumps({**plan, "max_sessions": plan["max_sessions"] + 1}), encoding="utf-8")

    assert (
        main(
            [*_flags(roots), "apply", "--plan-file", str(plan_file), "--confirmation-token", token,
             "--now-ms", str(ARMED_AT_MS)],
            settings=SETTINGS,
        )
        == 2
    )

    refusal = _last_object(capsys)
    assert refusal["error"] == "LIVE_ARMING_TOKEN_INVALID"
    assert "content hash does not verify" in refusal["detail"]
    assert LiveArmingLedger(artifacts_root, live_account_id=LIVE_ACCT).records() == ()


def test_a_plan_file_that_cannot_be_written_is_one_json_refusal_at_exit_one(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """``--plan-out`` under a regular file is a refusal, not a traceback.

    The documented contract is one JSON object per invocation, so an
    ``OSError`` out of the atomic publish must not replace it -- and the plan
    itself is not printed beside the error, or operator automation would read
    two objects and a non-zero exit for the same run.
    """
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    blocker = artifacts_root / "not-a-directory"
    blocker.write_text("", encoding="utf-8")

    assert (
        main(
            [*_flags(roots), "plan", "--strategy-instance-id", ARMING_SID,
             "--plan-out", str(blocker / "plan.json"), "--now-ms", str(ARMED_AT_MS)],
            settings=SETTINGS,
        )
        == 1
    )

    printed = capsys.readouterr().out.splitlines()
    assert len(printed) == 1
    refusal = json.loads(printed[0])
    assert "cannot write the plan file" in refusal["error"]
    assert refusal["submission_admitted"] is False
    assert LiveArmingLedger(artifacts_root, live_account_id=LIVE_ACCT).records() == ()


def test_status_for_an_instance_without_a_binding_refuses_instead_of_using_another_account(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """A named instance cannot inherit the account-wide Shadow activation."""
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    _arm(roots, capsys)

    assert (
        main(
            [*_flags(roots), "status", "--strategy-instance-id", "never-seen",
             "--now-ms", str(ARMED_AT_MS)],
            settings=SETTINGS,
        )
        == 2
    )

    refusal = _last_object(capsys)
    assert refusal["error"] == LIVE_ARMING_INSTANCE_UNSEALED
    assert "never-seen has no sealed alpaca binding" in refusal["detail"]


def test_a_confirmation_token_that_is_not_ascii_refuses_at_exit_two(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """``secrets.compare_digest`` raises on a non-ASCII string; the ceremony refuses first."""
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
            [*_flags(roots), "apply", "--plan-file", str(plan_file), "--confirmation-token", "töken",
             "--now-ms", str(ARMED_AT_MS)],
            settings=SETTINGS,
        )
        == 2
    )

    assert _last_object(capsys)["error"] == "LIVE_ARMING_TOKEN_INVALID"
    assert LiveArmingLedger(artifacts_root, live_account_id=LIVE_ACCT).records() == ()


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
        ledger.path.read_text(encoding="utf-8").replace(
            f'"armed_at_ms":{ARMED_AT_MS}', f'"armed_at_ms":{ARMED_AT_MS + 1}'
        ),
        encoding="utf-8",
    )

    assert main([*_flags(roots), "status", "--now-ms", str(ARMED_AT_MS)], settings=SETTINGS) == 1

    assert "digest does not verify" in _last_object(capsys)["error"]


def test_an_unverifiable_live_activation_refuses_even_when_shadow_evidence_exists(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """A Shadow candidate cannot hide corrupt evidence in the Live store."""
    artifacts_root, _live_state_root = roots
    activate_shadow_fence(artifacts_root)
    ledger_path = artifacts_root / "accounts" / "alpaca" / ACTIVATION_FILENAME
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text("not-json\n", encoding="utf-8")

    assert main([*_flags(roots), "status", "--now-ms", str(ARMED_AT_MS)], settings=SETTINGS) == 2

    refusal = _last_object(capsys)
    assert refusal["error"] == LIVE_ARMING_INSTANCE_UNSEALED
    assert "Live activation ledger cannot be verified" in refusal["detail"]


def test_a_real_activation_record_reports_submission_admitted(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI reports a verified activation record, not a booted authority.

    Whether the live authority actually installed is a boot-time fact this
    read-only command cannot observe, and the note names the one condition (an
    open control plane) that stops it (slice 7 R14).
    """
    artifacts_root, _live_state_root = roots
    activate_shadow_fence(artifacts_root)
    _graduate_live_account(artifacts_root)

    assert main([*_flags(roots), "status", "--now-ms", str(ARMED_AT_MS)], settings=SETTINGS) == 0

    report = _last_object(capsys)
    assert report["submission_admitted"] is True
    assert report["note"] == _SUBMISSION_ADMITTED_NOTE
    assert "a live activation record exists for this account" in report["note"]
    assert "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL=true" in report["note"]
