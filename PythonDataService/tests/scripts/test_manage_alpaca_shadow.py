"""The shadow operator CLI activates, lists sessions, and writes the receipt only when the gate holds."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.shadow_activation import ShadowActivationStore
from app.broker.alpaca.clerk.shadow_receipt import ShadowReceiptStore
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.services.alpaca_shadow_reconciliation import (
    ShadowGateEvaluation,
    ShadowSessionVerdict,
    TwinDayReconciliation,
)
from app.services.bot_binding_repository import (
    BrokerBotBinding,
    alpaca_v1_action_plan,
    live_state_binding_repository,
)
from app.utils.session_anchors import MAX_TIMESTAMP_MS
from scripts.manage_alpaca_shadow import main
from tests.broker.alpaca.clerk.sqlite.conftest import _clock_at

LIVE_ACCOUNT = "9LIVE0001"
SID, TWIN = "s", "t"
TWIN_ACCOUNT = "PA-TWIN-CLI"
JUDGED_AT_MS = 1_786_368_000_000


def _evaluation(counted: int, required: int) -> ShadowGateEvaluation:
    sessions = tuple(
        ShadowSessionVerdict(
            session_open_ms=1_000 + i,
            state="counted",
            detail="",
            shadow_run_id="run-1",
            reconciliation=TwinDayReconciliation(
                1_000 + i, SID, TWIN, (), (), (), None, None, Decimal("0.01")
            ),
        )
        for i in range(counted)
    )
    return ShadowGateEvaluation(
        live_account_id=LIVE_ACCOUNT,
        strategy_instance_id=SID,
        twin_account_id="PA-TEST",
        twin_strategy_instance_id=TWIN,
        configured_signal_hash="a" * 64,
        required_sessions=required,
        sessions=sessions,
    )


def _repeated_session_evaluation() -> ShadowGateEvaluation:
    """Two counted verdicts on one trading day — a shape ``ShadowReceipt.create`` refuses."""
    evaluation = _evaluation(2, 2)
    first, second = evaluation.sessions
    return replace(
        evaluation, sessions=(first, replace(second, session_open_ms=first.session_open_ms))
    )


def _never_called(**_kwargs: object) -> ShadowGateEvaluation:
    raise AssertionError("the gate must not be evaluated for this invocation")


def _record_binding(live_state_root: Path, strategy_instance_id: str) -> None:
    """Put one readable runner binding on disk — what the CLI reads before it judges."""
    live_state_binding_repository(live_state_root).record_launch(
        BrokerBotBinding(
            strategy_instance_id=strategy_instance_id,
            broker="alpaca",
            symbol="SPY",
            mode="trade",
            action_plan=alpaca_v1_action_plan("SPY"),
            run_id=f"{strategy_instance_id}-run-1",
            created_at_ms=1_757_000_000_000,
        ),
        launch_reason="deploy",
    )


def _last_object(capsys: pytest.CaptureFixture[str]) -> dict:
    return json.loads(capsys.readouterr().out.splitlines()[-1])


def test_activate_writes_the_fence_and_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [
        "--live-account-id",
        LIVE_ACCOUNT,
        "--artifacts-root",
        str(tmp_path),
        "--live-state-root",
        str(tmp_path / "live"),
        "activate",
    ]
    assert main(argv) == 0
    assert main(argv) == 0
    store = ShadowActivationStore(tmp_path)
    record = store.latest(f"shadow:{LIVE_ACCOUNT}")
    assert record is not None

    printed = capsys.readouterr().out.splitlines()
    # Byte-identical proofs: the second call re-read the first activation
    # rather than performing a second one.
    assert printed[0] == printed[1]
    assert json.loads(printed[-1])["account_id"] == f"shadow:{LIVE_ACCOUNT}"
    assert len(store.path.read_text().splitlines()) == 1


def test_the_receipt_is_written_only_by_receipt_and_only_when_the_gate_is_satisfied(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = [
        "--live-account-id",
        LIVE_ACCOUNT,
        "--artifacts-root",
        str(tmp_path),
        "--live-state-root",
        str(tmp_path / "live"),
    ]
    twin = [
        "--strategy-instance-id",
        SID,
        "--twin-account-id",
        "PA-TEST",
        "--twin-strategy-instance-id",
        TWIN,
        "--required-sessions",
        "2",
    ]
    _record_binding(tmp_path / "live", SID)
    _record_binding(tmp_path / "live", TWIN)

    # A gate ``sessions`` reports as *satisfied* is the only case that could
    # seal from the read-only command, so it is the case that has to be pinned.
    assert ShadowReceiptStore(tmp_path).all_for(SID) == ()
    assert main([*base, "sessions", *twin], evaluate=lambda **_kw: _evaluation(2, 2)) == 0
    assert ShadowReceiptStore(tmp_path).all_for(SID) == ()

    assert main([*base, "receipt", *twin], evaluate=lambda **_kw: _evaluation(1, 2)) == 2
    assert ShadowReceiptStore(tmp_path).latest(SID) is None
    assert _last_object(capsys)["satisfied"] is False

    assert main([*base, "receipt", *twin], evaluate=lambda **_kw: _evaluation(2, 2)) == 0
    receipt = ShadowReceiptStore(tmp_path).latest(SID)
    assert receipt is not None and len(receipt.sessions) == 2 and receipt.required_sessions == 2


def test_a_reserved_shadow_identity_is_refused_before_any_work(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [
        "--live-account-id",
        f"shadow:{LIVE_ACCOUNT}",
        "--artifacts-root",
        str(tmp_path),
        "--live-state-root",
        str(tmp_path / "live"),
        "activate",
    ]
    assert main(argv) == 1
    assert "reserved" in _last_object(capsys)["error"]
    assert ShadowActivationStore(tmp_path).latest(f"shadow:{LIVE_ACCOUNT}") is None


def test_an_unknown_binding_is_an_evidence_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _record_binding(tmp_path / "live", SID)
    argv = [
        "--live-account-id",
        LIVE_ACCOUNT,
        "--artifacts-root",
        str(tmp_path),
        "--live-state-root",
        str(tmp_path / "live"),
        "sessions",
        "--strategy-instance-id",
        SID,
        "--twin-account-id",
        "PA-TEST",
        "--twin-strategy-instance-id",
        TWIN,
        "--required-sessions",
        "2",
    ]

    assert main(argv, evaluate=_never_called) == 1
    assert TWIN in _last_object(capsys)["error"]


def test_the_paper_twin_binding_can_live_under_a_separate_runner_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The Paper Clerk root and runner root are independently configurable.

    A Paper host must not share the Shadow process's writable ``live_state``
    just so a read-only comparison can find its immutable binding.
    """
    shadow_runner = tmp_path / "shadow-runner"
    paper_runner = tmp_path / "paper-runner"
    _record_binding(shadow_runner, SID)
    _record_binding(paper_runner, TWIN)

    argv = [
        "--live-account-id",
        LIVE_ACCOUNT,
        "--artifacts-root",
        str(tmp_path),
        "--live-state-root",
        str(shadow_runner),
        "sessions",
        "--strategy-instance-id",
        SID,
        "--twin-account-id",
        "PA-TEST",
        "--twin-strategy-instance-id",
        TWIN,
        "--twin-live-state-root",
        str(paper_runner),
        "--required-sessions",
        "2",
    ]

    assert main(argv, evaluate=lambda **_kw: _evaluation(2, 2)) == 0
    report = _last_object(capsys)
    assert report["strategy_instance_id"] == SID
    assert report["twin_strategy_instance_id"] == TWIN


@pytest.mark.parametrize("operation", ["sessions", "receipt"])
@pytest.mark.parametrize(
    ("flag", "value", "bound"),
    [
        ("--required-sessions", "0", "at least 1"),
        ("--now-ms", "-1", str(MAX_TIMESTAMP_MS)),
        ("--now-ms", str(MAX_TIMESTAMP_MS + 1), str(MAX_TIMESTAMP_MS)),
    ],
)
def test_a_flag_outside_its_bound_is_a_usage_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    operation: str,
    flag: str,
    value: str,
    bound: str,
) -> None:
    """A gate judged on zero required sessions, or against a clock that is not an instant.

    ``--required-sessions 0`` is the dangerous one: nothing downstream refuses
    it on the ``sessions`` path, which would report a real-money arming
    precondition met on no evidence at all.
    """
    argv = [
        "--live-account-id",
        LIVE_ACCOUNT,
        "--artifacts-root",
        str(tmp_path),
        "--live-state-root",
        str(tmp_path / "live"),
        operation,
        "--strategy-instance-id",
        SID,
        "--twin-account-id",
        "PA-TEST",
        "--twin-strategy-instance-id",
        TWIN,
        flag,
        value,
    ]
    if flag != "--required-sessions":
        argv += ["--required-sessions", "2"]

    assert main(argv, evaluate=_never_called) == 1

    error = _last_object(capsys)["error"]
    assert flag in error and bound in error


@pytest.mark.parametrize(
    ("argv", "fragment"),
    [
        pytest.param([], "--live-account-id", id="missing-required-flag"),
        pytest.param(
            ["--live-account-id", LIVE_ACCOUNT, "nosuch"], "invalid choice", id="unknown-subcommand"
        ),
        pytest.param(
            [
                "--live-account-id",
                LIVE_ACCOUNT,
                "sessions",
                "--strategy-instance-id",
                SID,
                "--twin-account-id",
                "PA-TEST",
                "--twin-strategy-instance-id",
                TWIN,
                "--required-sessions",
                "not-a-number",
            ],
            "--required-sessions",
            id="bad-type-value",
        ),
        pytest.param(
            ["--live-account-id", LIVE_ACCOUNT, "activate", "--nope"],
            "unrecognized arguments",
            id="unknown-flag",
        ),
    ],
)
def test_every_usage_refusal_is_one_json_object_at_exit_one(
    capsys: pytest.CaptureFixture[str], argv: list[str], fragment: str
) -> None:
    """Exit ``1``, never argparse's bare ``2`` -- which "gate not satisfied" owns.

    A script reading only the exit code could not otherwise tell a typo from a
    real-money arming precondition that has not been met.

    The two argparse paths a reviewer reads as escaping ``exit_on_error=False``
    -- a missing required argument, and a leftover unrecognized one -- are both
    pinned here rather than argued about: on this runtime's Python they raise
    ``ArgumentError`` for ``main`` to translate, and the day that stops being
    true these rows fail instead of the CLI silently exiting ``2`` with no
    JSON at all.
    """
    assert main(argv, evaluate=_never_called) == 1

    assert fragment in _last_object(capsys)["error"]


def test_a_receipt_the_sealer_refuses_is_an_evidence_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The receipt store is the last word on its own shape, and its refusal is a sentence."""
    _record_binding(tmp_path / "live", SID)
    _record_binding(tmp_path / "live", TWIN)
    argv = [
        "--live-account-id",
        LIVE_ACCOUNT,
        "--artifacts-root",
        str(tmp_path),
        "--live-state-root",
        str(tmp_path / "live"),
        "receipt",
        "--strategy-instance-id",
        SID,
        "--twin-account-id",
        "PA-TEST",
        "--twin-strategy-instance-id",
        TWIN,
        "--required-sessions",
        "2",
    ]

    assert main(argv, evaluate=lambda **_kw: _repeated_session_evaluation()) == 1
    assert "repeats a session" in _last_object(capsys)["error"]
    assert ShadowReceiptStore(tmp_path).all_for(SID) == ()


def test_the_printed_report_names_its_clock_and_not_the_two_order_books(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--now-ms`` decides the whole judged range, so the report is reproducible from itself."""
    _record_binding(tmp_path / "live", SID)
    _record_binding(tmp_path / "live", TWIN)
    argv = [
        "--live-account-id",
        LIVE_ACCOUNT,
        "--artifacts-root",
        str(tmp_path),
        "--live-state-root",
        str(tmp_path / "live"),
        "sessions",
        "--strategy-instance-id",
        SID,
        "--twin-account-id",
        "PA-TEST",
        "--twin-strategy-instance-id",
        TWIN,
        "--required-sessions",
        "2",
        "--now-ms",
        str(JUDGED_AT_MS),
    ]

    assert main(argv, evaluate=lambda **_kw: _evaluation(2, 2)) == 0
    report = _last_object(capsys)
    assert report["now_ms"] == JUDGED_AT_MS
    # An allowlist, not a denylist: a field added to ``TwinDayReconciliation``
    # is a decision at ``_reconciliation_payload``, never a silent leak here.
    assert set(report["sessions"][0]["reconciliation"]) == {
        "session_open_ms",
        "strategy_instance_id",
        "twin_strategy_instance_id",
        "divergences",
        "max_fill_price_drift",
        "max_fill_time_drift_ms",
        "fill_price_atol",
        "report_sha256",
    }


def test_the_default_evaluator_judges_over_the_authorities_the_cli_derives(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No injected evaluator: the real seam, over two real custody databases.

    Unsealed bindings stop the judging at the twin-identity check, which is as
    far as this needs to go. Reaching it proves the ten keyword arguments
    ``main`` hands the seam are the ones ``evaluate_shadow_gate`` requires,
    that the derived paths name the databases ``activate`` and the twin
    authority actually created, and that both read-only projections opened.
    """
    base = [
        "--live-account-id",
        LIVE_ACCOUNT,
        "--artifacts-root",
        str(tmp_path),
        "--live-state-root",
        str(tmp_path / "live"),
    ]
    assert main([*base, "activate"]) == 0
    ClerkSqliteRepository.initialize(
        account_id=TWIN_ACCOUNT, artifacts_root=tmp_path, clock=_clock_at(JUDGED_AT_MS)
    ).close()
    _record_binding(tmp_path / "live", SID)
    _record_binding(tmp_path / "live", TWIN)

    assert (
        main(
            [
                *base,
                "sessions",
                "--strategy-instance-id",
                SID,
                "--twin-account-id",
                TWIN_ACCOUNT,
                "--twin-strategy-instance-id",
                TWIN,
                "--required-sessions",
                "2",
                "--now-ms",
                str(JUDGED_AT_MS),
            ]
        )
        == 2
    )
    assert _last_object(capsys) == {
        "error": "SHADOW_TWIN_MISMATCH",
        "detail": "both bindings must carry their sealed program",
    }


def test_a_twin_account_with_no_custody_database_is_an_evidence_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default evaluator's own refusal: an operator pointed at a root that holds nothing."""
    base = [
        "--live-account-id",
        LIVE_ACCOUNT,
        "--artifacts-root",
        str(tmp_path),
        "--live-state-root",
        str(tmp_path / "live"),
    ]
    assert main([*base, "activate"]) == 0
    _record_binding(tmp_path / "live", SID)
    _record_binding(tmp_path / "live", TWIN)

    assert (
        main(
            [
                *base,
                "sessions",
                "--strategy-instance-id",
                SID,
                "--twin-account-id",
                "PA-ABSENT",
                "--twin-strategy-instance-id",
                TWIN,
                "--required-sessions",
                "2",
                "--now-ms",
                str(JUDGED_AT_MS),
            ]
        )
        == 1
    )
    error = _last_object(capsys)["error"]
    assert error.startswith("twin custody database not found at") and "PA-ABSENT" in error
