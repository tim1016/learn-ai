"""Per-section unit tests for the ``operator_surface`` projection
(PRD #607, cockpit revision 2026-06-21).

The cockpit-revision contract:

- ``host_process.state`` is one of ``RUNNING / STOPPING / EXITED / IDLE
  / WAITING_FOR_HOST / UNREACHABLE``.  ``IDLE`` is the daemon-reachable-
  but-no-subprocess case; it upgrades to ``WAITING_FOR_HOST`` when the
  operator's durable intent is ``RUNNING``.
- ``broker`` carries two independent enums: ``safety_verdict``
  (``PAPER_ONLY / UNSAFE / UNKNOWN``) and ``connection`` (``CONNECTED /
  DISCONNECTED / UNKNOWN``).  Composing them is forbidden.
- ``trading_session`` is server-authored (phase + permission +
  next-transition + timezone + as_of_ms).
"""

from __future__ import annotations

import pytest

from app.broker.ibkr.account_truth_freshness import ACCOUNT_TRUTH_SOURCE_FRESHNESS_SPECS
from app.broker.ibkr.models import IbkrConnectionHealth
from app.engine.live.account_artifacts import AccountFreezeEvidence
from app.schemas.account_truth import (
    AccountTruthMessage,
    AccountTruthResponse,
    AccountTruthSourceFreshness,
)
from app.schemas.live_runs import (
    DesiredStateView,
    InstanceBrokerView,
    InstanceLastExit,
    InstanceProcessView,
    InstanceSizing,
    InstanceStartDefaults,
    LiveBinding,
    OperatorSurfaceAccountOwner,
    ReadinessGate,
    ReadinessVector,
)
from app.services import operator_surface as operator_surface_module
from app.services.account_truth_snapshot import AccountTruthSnapshot
from app.services.operator_capability import REASON_CODES, evaluate_action
from app.services.operator_surface import compute_operator_surface
from app.services.resume_guard_state import (
    BrokerSafetyArtifact,
    ReconciliationArtifact,
    SubmissionCapabilityArtifact,
    UncertainIntentArtifact,
    resolve_guard_state,
)
from app.services.runtime_freshness import DomainFreshness, RuntimeFreshness

_PROC = InstanceProcessView(state="running")
_IDLE_PROC = InstanceProcessView(state="idle")
_LIVE = LiveBinding(run_id="run-live-x")
_NOW_MS = 1_700_000_000_000


def _surface(**overrides):
    """Build a surface with sane defaults; tests override one section."""
    now_ms = overrides.get("now_ms", _NOW_MS)
    kwargs = {
        "process": _PROC,
        "now_ms": now_ms,
        "account_truth_snapshot": _account_truth_snapshot(generated_at_ms=now_ms - 1_000),
    }
    kwargs.update(overrides)
    return compute_operator_surface(**kwargs)


def _account_truth_snapshot(
    *,
    final_verdict: str = "clean",
    generated_at_ms: int = _NOW_MS - 1_000,
    blockers: list[AccountTruthMessage] | None = None,
) -> AccountTruthSnapshot:
    severity = "ok" if final_verdict == "clean" else "critical"
    truth = AccountTruthResponse(
        account_id="DU123",
        final_verdict=final_verdict,  # type: ignore[arg-type]
        final_severity=severity,  # type: ignore[arg-type]
        status_label="Clean" if final_verdict == "clean" else "Not proven",
        status_detail="Account Truth is clean." if final_verdict == "clean" else "Account Truth has blockers.",
        generated_at_ms=generated_at_ms,
        health=IbkrConnectionHealth(
            mode="paper",
            host="127.0.0.1",
            port=4002,
            client_id=7,
            connected=True,
            account_id="DU123",
            is_paper=True,
            fetched_at_ms=generated_at_ms,
            connection_state="connected",
            last_transition_ms=generated_at_ms,
        ),
        invariants=[],
        blockers=blockers or [],
        source_freshness=_fresh_source_freshness(generated_at_ms),
    )
    return AccountTruthSnapshot(truth=truth, cached_at_ms=generated_at_ms)


def _fresh_source_freshness(generated_at_ms: int) -> list[AccountTruthSourceFreshness]:
    return [
        AccountTruthSourceFreshness(
            source=spec.source,
            label=spec.label,
            status="fresh",
            severity=spec.severity,
            fetched_at_ms=generated_at_ms,
            age_ms=0,
            hard_ttl_ms=spec.hard_ttl_ms,
            reason_code=None,
            message=f"{spec.label} evidence is fresh.",
        )
        for spec in ACCOUNT_TRUTH_SOURCE_FRESHNESS_SPECS
    ]


def _guard(
    *,
    broker_state: str = "SAFE",
    submission_state: str = "SATISFIED",
    reconciliation_state: str = "PASSED",
    uncertain_state: str = "CLEAR",
    unresolved_intent_ids: tuple[str, ...] = (),
):
    return resolve_guard_state(
        broker_safety=BrokerSafetyArtifact(state=broker_state),  # type: ignore[arg-type]
        submission_capability=SubmissionCapabilityArtifact(state=submission_state),  # type: ignore[arg-type]
        reconciliation=ReconciliationArtifact(state=reconciliation_state),  # type: ignore[arg-type]
        uncertain_intent=UncertainIntentArtifact(
            state=uncertain_state,  # type: ignore[arg-type]
            unresolved_intent_ids=unresolved_intent_ids,
        ),
    )


def _owner(
    *,
    phase: str = "accepting",
    generation: int | None = 4,
) -> OperatorSurfaceAccountOwner:
    return OperatorSurfaceAccountOwner(
        account_id="DU123",
        generation=generation,
        phase=phase,  # type: ignore[arg-type]
        recorded_at_ms=_NOW_MS - 10_000,
        source="account_owner",
    )


def _runtime_freshness(effective_posture: str = "PAPER_EXECUTION") -> RuntimeFreshness:
    fresh = DomainFreshness(state="FRESH", age_ms=100)
    return RuntimeFreshness(
        command_loop=fresh,
        broker=fresh,
        bar_loop=fresh,
        control_plane=fresh,
        posture_demoted=False,
        effective_posture=effective_posture,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# host_process — 5 base states + WAITING_FOR_HOST derivation
# ---------------------------------------------------------------------------


def _desired(state: str | None) -> DesiredStateView | None:
    if state is None:
        return None
    return DesiredStateView(state=state, path_status="ok")


@pytest.mark.parametrize(
    ("daemon_state", "expected", "expects_notice"),
    [
        ("running", "RUNNING", False),
        ("stopping", "STOPPING", True),
        ("exited", "EXITED", True),
        ("idle", "IDLE", True),
        ("unreachable", "UNREACHABLE", True),
        ("nonsense", "UNREACHABLE", True),
    ],
)
def test_host_process_base_state_mapping(daemon_state: str, expected: str, expects_notice: bool) -> None:
    surface = _surface(process=InstanceProcessView(state=daemon_state))
    assert surface.host_process.state == expected
    if expects_notice:
        assert surface.host_process.notice and isinstance(surface.host_process.notice, str)
    else:
        assert surface.host_process.notice is None
    assert surface.host_process.copyable_command is None


def test_host_process_idle_plus_desired_running_becomes_waiting_for_host() -> None:
    surface = _surface(process=_IDLE_PROC, desired_state=_desired("RUNNING"))
    assert surface.host_process.state == "WAITING_FOR_HOST"
    assert surface.host_process.notice is not None
    assert "Trading was requested" in surface.host_process.notice


def test_host_process_idle_without_desired_running_stays_idle() -> None:
    # No durable intent at all -> IDLE.
    surface = _surface(process=_IDLE_PROC, desired_state=None)
    assert surface.host_process.state == "IDLE"

    # Durable PAUSED -> IDLE (operator has not asked it to run).
    surface = _surface(process=_IDLE_PROC, desired_state=_desired("PAUSED"))
    assert surface.host_process.state == "IDLE"


def test_host_process_running_ignores_desired_state_override() -> None:
    # The state enum reflects DAEMON reality; desired-state only
    # upgrades IDLE -> WAITING_FOR_HOST, never overrides RUNNING.
    surface = _surface(process=_PROC, desired_state=_desired("PAUSED"))
    assert surface.host_process.state == "RUNNING"


# ---------------------------------------------------------------------------
# host_process — copyable_command (ADR 0013 amendment 2026-06-22)
# ---------------------------------------------------------------------------


_HOST_CMD = "./start-live-daemon.sh --background"


def test_host_process_unreachable_with_configured_command_emits_it() -> None:
    surface = _surface(
        process=InstanceProcessView(state="unreachable"),
        host_start_command=_HOST_CMD,
    )
    assert surface.host_process.state == "UNREACHABLE"
    assert surface.host_process.copyable_command == _HOST_CMD


def test_host_process_unreachable_without_configured_command_stays_none() -> None:
    # Empty string -> no safe command can be authored -> emit None and
    # let the cockpit fall back to a runbook remediation.
    surface = _surface(
        process=InstanceProcessView(state="unreachable"),
        host_start_command="",
    )
    assert surface.host_process.state == "UNREACHABLE"
    assert surface.host_process.copyable_command is None


def test_host_process_unreachable_with_none_command_stays_none() -> None:
    # Default (no setting passed) also produces None.
    surface = _surface(process=InstanceProcessView(state="unreachable"))
    assert surface.host_process.state == "UNREACHABLE"
    assert surface.host_process.copyable_command is None


@pytest.mark.parametrize(
    "daemon_state",
    ["running", "stopping", "exited", "idle"],
)
def test_host_process_non_unreachable_never_emits_daemon_command(daemon_state: str) -> None:
    # The daemon-start command must not leak outside UNREACHABLE — for
    # EXITED / IDLE / WAITING_FOR_HOST, restarting the host service does
    # not restart the per-bot subprocess and would mislead the trader.
    surface = _surface(
        process=InstanceProcessView(state=daemon_state),
        host_start_command=_HOST_CMD,
    )
    assert surface.host_process.state != "UNREACHABLE"
    assert surface.host_process.copyable_command is None


def test_host_process_waiting_for_host_never_emits_daemon_command() -> None:
    # IDLE + durable RUNNING -> WAITING_FOR_HOST; same rule applies.
    surface = _surface(
        process=_IDLE_PROC,
        desired_state=_desired("RUNNING"),
        host_start_command=_HOST_CMD,
    )
    assert surface.host_process.state == "WAITING_FOR_HOST"
    assert surface.host_process.copyable_command is None


# ---------------------------------------------------------------------------
# host_process — start_capability (ADR 0013 amendment 2026-06-22, slice 3)
# ---------------------------------------------------------------------------


_START_RUN = "run-evidence-x"


def _defaults(strategy: str = "spy_ema_crossover") -> InstanceStartDefaults:
    return InstanceStartDefaults(strategy=strategy, readonly=True, max_orders_per_day=50)


@pytest.mark.parametrize(
    ("daemon_state", "want_state"),
    [
        ("idle", "IDLE"),
        ("exited", "EXITED"),
    ],
)
def test_host_process_start_capability_enabled_for_startable_states(daemon_state: str, want_state: str) -> None:
    surface = _surface(
        process=InstanceProcessView(state=daemon_state),
        start_run_id=_START_RUN,
        start_defaults=_defaults(),
    )
    assert surface.host_process.state == want_state
    cap = surface.host_process.start_capability
    assert cap.enabled is True
    assert cap.run_id == _START_RUN
    assert cap.disabled_reason_code is None
    assert cap.request is not None
    assert cap.request.strategy == "spy_ema_crossover"
    assert cap.request.readonly is True
    assert cap.request.max_orders_per_day == 50


def test_host_process_start_capability_enabled_for_waiting_for_host() -> None:
    surface = _surface(
        process=_IDLE_PROC,
        desired_state=_desired("RUNNING"),
        start_run_id=_START_RUN,
        start_defaults=_defaults(),
    )
    assert surface.host_process.state == "WAITING_FOR_HOST"
    assert surface.host_process.start_capability.enabled is True
    assert surface.host_process.start_capability.run_id == _START_RUN


@pytest.mark.parametrize(
    ("daemon_state", "want_state", "want_reason"),
    [
        ("running", "RUNNING", "ALREADY_RUNNING"),
        ("stopping", "STOPPING", "STOPPING"),
        ("unreachable", "UNREACHABLE", "HOST_SERVICE_OFFLINE"),
    ],
)
def test_host_process_start_capability_disabled_per_state(daemon_state: str, want_state: str, want_reason: str) -> None:
    # Even with valid start inputs, these states block Start.
    surface = _surface(
        process=InstanceProcessView(state=daemon_state),
        start_run_id=_START_RUN,
        start_defaults=_defaults(),
    )
    assert surface.host_process.state == want_state
    cap = surface.host_process.start_capability
    assert cap.enabled is False
    assert cap.disabled_reason_code == want_reason
    assert cap.run_id is None
    assert cap.request is None
    assert len(cap.gate_results) == 1
    gate = cap.gate_results[0]
    assert gate.gate_id == "start.daemon_state"
    assert gate.status == "block"
    assert gate.source == "operator_surface"
    assert gate.operator_reason == want_reason
    assert gate.operator_next_step == want_reason
    assert gate.evidence_at_ms == _NOW_MS


def test_host_process_start_capability_intent_stopped_overrides_state() -> None:
    # Permanent retirement via durable STOPPED outranks every per-state
    # guard, even for a process state that would otherwise be startable.
    surface = _surface(
        process=InstanceProcessView(state="exited"),
        desired_state=_desired("STOPPED"),
        start_run_id=_START_RUN,
        start_defaults=_defaults(),
    )
    assert surface.host_process.state == "EXITED"
    cap = surface.host_process.start_capability
    assert cap.enabled is False
    assert cap.disabled_reason_code == "STOPPED_REQUIRES_REDEPLOY"


def test_host_process_start_capability_poisoned_overrides_state() -> None:
    surface = _surface(
        process=_IDLE_PROC,
        poisoned=True,
        start_run_id=_START_RUN,
        start_defaults=_defaults(),
    )
    cap = surface.host_process.start_capability
    assert cap.enabled is False
    assert cap.disabled_reason_code == "STOPPED_REQUIRES_REDEPLOY"


@pytest.mark.parametrize(
    ("start_run_id", "start_defaults"),
    [
        (None, _defaults()),
        (_START_RUN, None),
        (_START_RUN, InstanceStartDefaults(strategy="")),
    ],
    ids=["no-run-id", "no-defaults", "empty-strategy"],
)
def test_host_process_start_capability_disabled_for_incomplete_settings(
    start_run_id: str | None, start_defaults: InstanceStartDefaults | None
) -> None:
    surface = _surface(
        process=_IDLE_PROC,
        start_run_id=start_run_id,
        start_defaults=start_defaults,
    )
    cap = surface.host_process.start_capability
    assert cap.enabled is False
    assert cap.disabled_reason_code == "START_SETTINGS_INCOMPLETE"
    assert cap.run_id is None
    assert cap.request is None


def test_host_process_start_capability_disabled_when_request_validation_fails() -> None:
    # Saved start settings whose ``strategy`` is non-empty but violates the
    # ``HostRunnerStartRequest.strategy`` pattern (``^[a-z][a-z0-9_]{0,63}$``)
    # would otherwise raise ValidationError mid-projection and 500 the
    # operator surface. Fail-closed: route to the same "settings incomplete"
    # disabled state the function emits for empty/missing settings.
    bad_defaults = InstanceStartDefaults(strategy="INVALID-STRATEGY")
    surface = _surface(
        process=_IDLE_PROC,
        start_run_id=_START_RUN,
        start_defaults=bad_defaults,
    )
    cap = surface.host_process.start_capability
    assert cap.enabled is False
    assert cap.disabled_reason_code == "START_SETTINGS_INCOMPLETE"
    assert cap.run_id is None
    assert cap.request is None


# ---------------------------------------------------------------------------
# prior_run
# ---------------------------------------------------------------------------


def _exit(**overrides):
    base: dict = {"run_id": "run-x"}
    base.update(overrides)
    return InstanceLastExit(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("last_exit", "expected"),
    [
        (_exit(halt_trigger="OUTSIDE_MUTATION", exit_code=1), "HALT_TRIGGERED"),
        (_exit(halt_trigger="OPERATOR_DECLARED"), "HALT_TRIGGERED"),
        (_exit(exit_code=0), "CLEAN"),
        (_exit(exit_reason="normal"), "CLEAN"),
        (_exit(exit_code=0, exit_reason="normal"), "CLEAN"),
        (_exit(exit_code=1), "EXITED_WITH_ERROR"),
        (_exit(exit_code=137), "EXITED_WITH_ERROR"),
        (_exit(), "UNKNOWN"),
        (None, "UNKNOWN"),
    ],
)
def test_prior_run_classification_mapping(last_exit, expected) -> None:
    assert _surface(last_exit=last_exit).prior_run.classification == expected


# ---------------------------------------------------------------------------
# broker — safety_verdict and connection are INDEPENDENT
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("safety_verdict_final", "expected_verdict"),
    [
        ("paper-only", "PAPER_ONLY"),
        ("unsafe", "UNSAFE"),
        ("unknown", "UNKNOWN"),
        (None, "UNKNOWN"),
    ],
)
def test_broker_safety_verdict_consumes_reactive_final_verdict(safety_verdict_final, expected_verdict) -> None:
    # PRD #616: the safety verdict now consumes ADR-0011's reactive
    # ``BrokerSafetyVerdict.final_verdict`` instead of ``configured_mode``,
    # so a mid-session degradation flips the cockpit's SAFETY pill
    # immediately.  Independent of connection state.
    for connection_state in ("connected", "disconnected", "unknown", None):
        surface = _surface(
            safety_verdict_final=safety_verdict_final,
            broker_connection_state=connection_state,
        )
        assert surface.broker.safety_verdict == expected_verdict


@pytest.mark.parametrize(
    ("connection_state", "expected_connection"),
    [
        ("connected", "CONNECTED"),
        ("disconnected", "DISCONNECTED"),
        ("degraded", "DEGRADED"),
        ("unknown", "UNKNOWN"),
        (None, "UNKNOWN"),
    ],
)
def test_broker_connection_independent_of_safety(connection_state, expected_connection) -> None:
    for safety_verdict_final in ("paper-only", "unsafe", "unknown", None):
        surface = _surface(
            safety_verdict_final=safety_verdict_final,
            broker_connection_state=connection_state,
        )
        assert surface.broker.connection == expected_connection


# ---------------------------------------------------------------------------
# execution — backend-authored translation of engine effective_posture
# ---------------------------------------------------------------------------


def test_execution_absent_without_runtime_evidence() -> None:
    assert _surface(runtime_freshness=None).execution is None


@pytest.mark.parametrize(
    ("engine_posture", "expected_trader_posture"),
    [
        ("PAPER_EXECUTION", "PAPER_EXECUTION"),
        ("PAPER_OBSERVATION", "READ_ONLY"),
        ("UNSAFE", "UNSAFE"),
        ("UNKNOWN", "UNKNOWN"),
    ],
)
def test_execution_posture_translates_engine_effective_posture(
    engine_posture: str,
    expected_trader_posture: str,
) -> None:
    surface = _surface(runtime_freshness=_runtime_freshness(engine_posture))
    assert surface.execution is not None
    assert surface.execution.posture == expected_trader_posture


def test_execution_posture_unknown_engine_member_is_loud() -> None:
    with pytest.raises(AssertionError, match="Unhandled engine effective posture"):
        operator_surface_module._trader_execution_posture("LIVE_EXECUTION")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# trader_guidance + submit_readiness (PRD #718)
# ---------------------------------------------------------------------------


def test_trader_guidance_safe_to_submit_requires_all_proofs() -> None:
    surface = _surface(
        safety_verdict_final="paper-only",
        broker_connection_state="connected",
        runtime_freshness=_runtime_freshness(),
        guard_state=_guard(),
        account_owner=_owner(),
        reconciliation_receipt=_make_receipt(status="passed", outcome="clean"),
        now_ms=_RTH_MID,
    )

    assert surface.submit_readiness.code == "safe_to_submit"
    assert surface.submit_readiness.can_submit is True
    assert surface.submit_readiness.blocking_reason_codes == []
    assert surface.trader_guidance.situation_code == "ready_to_submit"
    assert surface.trader_guidance.primary_remediation.kind == "none"
    assert surface.trader_guidance.additional_attention_groups == []
    evidence = {fact.label: fact for fact in surface.trader_guidance.advanced_evidence}
    assert evidence["account_owner.generation"].value == "4"
    assert evidence["broker.safety_verdict"].value == "PAPER_ONLY"
    assert evidence["reconciliation.state"].value == "CLEAN"
    proof_lines = {line.id: line for line in surface.trader_guidance.proof_lines}
    assert list(proof_lines) == [
        "broker-proof",
        "submit-readiness",
        "account-owner",
        "reconciliation",
        "runtime-freshness",
    ]
    assert proof_lines["broker-proof"].message == "Paper broker is connected."
    assert proof_lines["broker-proof"].detail == "Paper-only account proof is present. Broker session is connected."
    assert proof_lines["broker-proof"].tone == "ok"
    assert proof_lines["runtime-freshness"].message == "Runtime evidence is fresh."
    assert proof_lines["runtime-freshness"].tone == "ok"


def test_trader_guidance_missing_account_truth_cache_is_not_safe_to_submit() -> None:
    surface = _surface(
        safety_verdict_final="paper-only",
        broker_connection_state="connected",
        runtime_freshness=_runtime_freshness(),
        guard_state=_guard(),
        account_owner=_owner(),
        reconciliation_receipt=_make_receipt(status="passed", outcome="clean"),
        account_truth_snapshot=None,
        now_ms=_RTH_MID,
    )

    assert surface.submit_readiness.code == "broker_state_unproven"
    assert surface.submit_readiness.can_submit is False
    assert "ACCOUNT_TRUTH_NOT_AVAILABLE" in surface.submit_readiness.blocking_reason_codes
    attention = next(
        group
        for group in surface.trader_guidance.additional_attention_groups
        if group.code == "account_truth"
    )
    assert attention.headline == "Account Truth snapshot is unavailable"


def test_trader_guidance_stale_account_truth_cache_is_not_safe_to_submit() -> None:
    surface = _surface(
        safety_verdict_final="paper-only",
        broker_connection_state="connected",
        runtime_freshness=_runtime_freshness(),
        guard_state=_guard(),
        account_owner=_owner(),
        reconciliation_receipt=_make_receipt(status="passed", outcome="clean"),
        account_truth_snapshot=_account_truth_snapshot(generated_at_ms=_NOW_MS - 60_001),
        now_ms=_NOW_MS,
    )

    assert surface.submit_readiness.code == "broker_state_unproven"
    assert surface.submit_readiness.can_submit is False
    assert "ACCOUNT_TRUTH_STALE" in surface.submit_readiness.blocking_reason_codes
    attention = next(
        group
        for group in surface.trader_guidance.additional_attention_groups
        if group.code == "account_truth"
    )
    assert attention.headline == "Account Truth snapshot is stale"
    assert "hard freshness threshold" in attention.explanation


def test_trader_guidance_not_clean_account_truth_cache_is_not_safe_to_submit() -> None:
    blocker = AccountTruthMessage(
        code="unknown_positions",
        severity="critical",
        title="Unknown current broker positions",
        message="At least one current IBKR position is not explained by known bot/manual evidence.",
    )
    surface = _surface(
        safety_verdict_final="paper-only",
        broker_connection_state="connected",
        runtime_freshness=_runtime_freshness(),
        guard_state=_guard(),
        account_owner=_owner(),
        reconciliation_receipt=_make_receipt(status="passed", outcome="clean"),
        account_truth_snapshot=_account_truth_snapshot(
            final_verdict="not_proven",
            generated_at_ms=_RTH_MID - 1_000,
            blockers=[blocker],
        ),
        now_ms=_RTH_MID,
    )

    assert surface.submit_readiness.code == "broker_state_unproven"
    assert surface.submit_readiness.can_submit is False
    assert "ACCOUNT_TRUTH_NOT_PROVEN" in surface.submit_readiness.blocking_reason_codes
    assert "ACCOUNT_TRUTH_UNKNOWN_POSITIONS" in surface.submit_readiness.blocking_reason_codes
    attention = next(
        group
        for group in surface.trader_guidance.additional_attention_groups
        if group.code == "account_truth"
    )
    assert attention.headline == "Account Truth is not clean"
    assert attention.explanation == blocker.message


def test_trader_guidance_runtime_market_closed_uses_notice_copy() -> None:
    fresh = DomainFreshness(state="FRESH", age_ms=100)
    runtime = RuntimeFreshness(
        command_loop=fresh,
        broker=fresh,
        bar_loop=DomainFreshness(
            state="STALE",
            age_ms=90_000,
            stale_reason_codes=["BAR_LOOP_SESSION_CLOSED"],
        ),
        control_plane=fresh,
        posture_demoted=False,
    )

    surface = _surface(
        safety_verdict_final="paper-only",
        broker_connection_state="connected",
        runtime_freshness=runtime,
        guard_state=_guard(),
        account_owner=_owner(),
        reconciliation_receipt=_make_receipt(status="passed", outcome="clean"),
        now_ms=_RTH_MID,
    )

    proof_lines = {line.id: line for line in surface.trader_guidance.proof_lines}
    runtime_line = proof_lines["runtime-freshness"]
    assert (
        runtime_line.message
        == "The bot is idle until the regular trading session opens. No trading decision is being made."
    )
    assert runtime_line.detail == "Market closed"
    assert runtime_line.tone == "neutral"


def test_trader_guidance_account_freeze_is_never_collapsed() -> None:
    freeze = AccountFreezeEvidence(
        account_id="DU123",
        reason="watchdog.flatten_failed",
        source="watchdog_halt_executor",
        recorded_at_ms=_NOW_MS,
        operator_next_step="CHECK_IBKR",
    )

    surface = _surface(
        safety_verdict_final="paper-only",
        broker_connection_state="connected",
        guard_state=_guard(),
        account_owner=_owner(),
        account_freeze=freeze,
        reconciliation_receipt=_make_receipt(status="passed", outcome="clean"),
    )

    assert surface.submit_readiness.code == "account_frozen"
    assert surface.submit_readiness.can_submit is False
    assert "ACCOUNT_FROZEN" in surface.submit_readiness.blocking_reason_codes
    assert surface.trader_guidance.situation_code == "account_frozen"
    assert surface.trader_guidance.primary_remediation.kind == "open_runbook"
    assert surface.trader_guidance.primary_remediation.slug == "watchdog-halt"
    assert any(group.code == "account_frozen" for group in surface.trader_guidance.additional_attention_groups)


def test_trader_guidance_submit_uncertainty_routes_to_reconcile_endpoint() -> None:
    surface = _surface(
        safety_verdict_final="paper-only",
        broker_connection_state="connected",
        guard_state=_guard(
            uncertain_state="PRESENT",
            unresolved_intent_ids=("intent-1",),
        ),
        account_owner=_owner(),
        reconciliation_receipt=_make_receipt(status="passed", outcome="clean"),
    )

    assert surface.submit_readiness.code == "submit_outcome_uncertain"
    assert surface.trader_guidance.situation_code == "submit_outcome_uncertain"
    assert "UNRESOLVED_UNCERTAIN_INTENT" in surface.submit_readiness.blocking_reason_codes
    assert surface.trader_guidance.primary_remediation.kind == "invoke_endpoint"
    assert surface.trader_guidance.primary_remediation.endpoint == "reconcile_instance"
    attention = next(
        group
        for group in surface.trader_guidance.additional_attention_groups
        if group.code == "submit_outcome_uncertain"
    )
    assert attention.remediation.kind == "invoke_endpoint"
    assert attention.remediation.endpoint == "reconcile_instance"


def test_trader_guidance_missing_owner_generation_is_waiting_not_safe() -> None:
    surface = _surface(
        safety_verdict_final="paper-only",
        broker_connection_state="connected",
        guard_state=_guard(),
        account_owner=_owner(phase="unknown", generation=None),
        reconciliation_receipt=_make_receipt(status="passed", outcome="clean"),
    )

    assert surface.submit_readiness.code == "waiting_for_owner_generation"
    assert surface.submit_readiness.can_submit is False
    assert "ACCOUNT_OWNER_GENERATION_UNPROVEN" in surface.submit_readiness.blocking_reason_codes
    assert surface.trader_guidance.situation_code == "waiting_for_owner_generation"
    assert any(group.code == "account_owner" for group in surface.trader_guidance.additional_attention_groups)
    proof_lines = {line.id: line for line in surface.trader_guidance.proof_lines}
    assert proof_lines["account-owner"].message == "Waiting for AccountOwner generation."
    assert proof_lines["account-owner"].tone == "attention"


def test_trader_guidance_reconciliation_not_available_cannot_be_safe_to_submit() -> None:
    surface = _surface(
        safety_verdict_final="paper-only",
        broker_connection_state="connected",
        guard_state=_guard(),
        account_owner=_owner(),
        reconciliation_receipt=None,
    )

    assert surface.submit_readiness.code == "broker_state_unproven"
    assert surface.submit_readiness.can_submit is False
    assert "RECONCILIATION_NOT_AVAILABLE" in surface.submit_readiness.blocking_reason_codes
    assert surface.trader_guidance.primary_remediation.kind == "invoke_endpoint"
    attention = next(
        group
        for group in surface.trader_guidance.additional_attention_groups
        if group.code == "reconciliation"
    )
    assert attention.remediation.kind == "invoke_endpoint"
    assert attention.remediation.endpoint == "reconcile_instance"


def test_trader_guidance_reconciliation_waits_for_live_runtime_when_process_exited() -> None:
    surface = _surface(
        process=InstanceProcessView(state="exited"),
        safety_verdict_final="paper-only",
        broker_connection_state="connected",
        guard_state=_guard(),
        account_owner=_owner(),
        reconciliation_receipt=None,
    )

    attention = next(
        group
        for group in surface.trader_guidance.additional_attention_groups
        if group.code == "reconciliation"
    )
    assert attention.headline == "Runtime reconciliation is waiting for a live bot process"
    assert attention.remediation.kind == "none"
    assert "runtime reconciliation cannot run" in attention.explanation


def test_trader_guidance_disconnected_broker_reconnects_before_reconcile() -> None:
    surface = _surface(
        safety_verdict_final="paper-only",
        broker_connection_state="disconnected",
        guard_state=_guard(),
        account_owner=_owner(),
        reconciliation_receipt=None,
    )

    assert surface.submit_readiness.code == "broker_state_unproven"
    assert "BROKER_CONNECTION_DISCONNECTED" in surface.submit_readiness.blocking_reason_codes
    assert "RECONCILIATION_NOT_AVAILABLE" in surface.submit_readiness.blocking_reason_codes
    assert surface.trader_guidance.primary_remediation.kind == "open_runbook"
    assert surface.trader_guidance.primary_remediation.slug == "broker-reconnect"
    attention = next(
        group
        for group in surface.trader_guidance.additional_attention_groups
        if group.code == "broker_connection"
    )
    assert attention.remediation.kind == "open_runbook"
    assert attention.remediation.slug == "broker-reconnect"


def test_trader_guidance_broker_connection_unknown_has_no_action_without_live_runtime() -> None:
    surface = _surface(
        process=InstanceProcessView(state="exited"),
        safety_verdict_final="paper-only",
        broker_connection_state="unknown",
        guard_state=_guard(),
        account_owner=_owner(),
        reconciliation_receipt=_make_receipt(status="passed", outcome="clean"),
    )

    attention = next(
        group
        for group in surface.trader_guidance.additional_attention_groups
        if group.code == "broker_connection"
    )
    assert attention.remediation.kind == "none"
    assert "no live runtime" in attention.explanation


def test_trader_guidance_degraded_broker_preserves_recovering_copy() -> None:
    surface = _surface(
        safety_verdict_final="paper-only",
        broker_connection_state="degraded",
        guard_state=_guard(),
        account_owner=_owner(),
        reconciliation_receipt=None,
    )

    assert surface.broker.connection == "DEGRADED"
    assert "BROKER_CONNECTION_DEGRADED" in surface.submit_readiness.blocking_reason_codes
    attention = next(
        group
        for group in surface.trader_guidance.additional_attention_groups
        if group.code == "broker_connection"
    )
    assert attention.headline == "Broker connection is recovering"
    broker_proof = next(line for line in surface.trader_guidance.proof_lines if line.id == "broker-proof")
    assert broker_proof.message == "Paper broker is configured, but the session is recovering."


# ---------------------------------------------------------------------------
# current_risk
# ---------------------------------------------------------------------------


def _broker(**overrides) -> InstanceBrokerView:
    base: dict = {"bot_order_namespace": "ns", "owned_positions": {}, "pending_order_count": 0}
    base.update(overrides)
    return InstanceBrokerView(**base)  # type: ignore[arg-type]


def test_current_risk_broker_none_renders_unknown_with_nulls() -> None:
    surface = _surface(broker=None)
    assert surface.current_risk.posture == "UNKNOWN"
    assert surface.current_risk.pending_order_count is None
    assert surface.current_risk.verdict == "UNKNOWN"
    assert surface.current_risk.unrealized_pnl is None


@pytest.mark.parametrize(
    ("owned", "expected_posture"),
    [
        ({}, "FLAT"),
        ({"SPY": 0}, "FLAT"),
        ({"SPY": 1}, "LONG"),
        ({"SPY": 10, "QQQ": 5}, "LONG"),
        ({"SPY": -3}, "SHORT"),
        ({"SPY": -1, "QQQ": -2}, "SHORT"),
        ({"SPY": 1, "QQQ": -1}, "MIXED"),
        ({"SPY": 0, "QQQ": -1}, "SHORT"),
    ],
)
def test_current_risk_posture_derived_from_owned_positions(owned, expected_posture) -> None:
    surface = _surface(broker=_broker(owned_positions=owned))
    assert surface.current_risk.posture == expected_posture


@pytest.mark.parametrize(
    ("owned", "pending", "expected_verdict"),
    [
        ({}, 0, "READY"),
        ({}, 3, "ATTENTION"),
        ({"SPY": 1}, 0, "ATTENTION"),
        ({"SPY": 1}, 2, "ATTENTION"),
    ],
)
def test_current_risk_verdict_rule(owned, pending, expected_verdict) -> None:
    surface = _surface(broker=_broker(owned_positions=owned, pending_order_count=pending))
    assert surface.current_risk.verdict == expected_verdict


def test_current_risk_pending_order_count_zero_vs_null_distinction() -> None:
    assert _surface(broker=None).current_risk.pending_order_count is None
    assert _surface(broker=_broker(pending_order_count=0)).current_risk.pending_order_count == 0


def test_current_risk_unrealized_pnl_passes_through_broker_field() -> None:
    surface = _surface(broker=_broker(unrealized_pnl=-1234.56))
    assert surface.current_risk.unrealized_pnl == -1234.56


# ---------------------------------------------------------------------------
# daily_order_cap
# ---------------------------------------------------------------------------


def _readiness(**overrides) -> ReadinessVector:
    base: dict = {
        "kind": "live_readiness",
        "as_of_ms": 0,
        "source": "engine",
        "verdict": "READY",
        "summary": "",
        "gates": [],
    }
    base.update(overrides)
    return ReadinessVector(**base)  # type: ignore[arg-type]


def test_daily_order_cap_null_when_no_readiness() -> None:
    surface = _surface(readiness=None)
    assert surface.daily_order_cap.used is None
    assert surface.daily_order_cap.limit is None


def test_daily_order_cap_reads_structured_fields_not_prose() -> None:
    readiness = _readiness(
        orders_used=7,
        orders_cap=50,
        gates=[
            ReadinessGate(name="orders_cap", status="pass", severity="hard", detail="LIES 99 / 1"),
        ],
    )
    surface = _surface(readiness=readiness)
    assert surface.daily_order_cap.used == 7
    assert surface.daily_order_cap.limit == 50


def test_daily_order_cap_null_when_engine_did_not_emit_structured_fields() -> None:
    surface = _surface(readiness=_readiness())
    assert surface.daily_order_cap.used is None
    assert surface.daily_order_cap.limit is None


# ---------------------------------------------------------------------------
# action_plan
# ---------------------------------------------------------------------------


def test_action_plan_null_yields_unknown_unknown() -> None:
    surface = _surface(action_plan=None)
    assert surface.action_plan.consumption == "UNKNOWN"
    assert surface.action_plan.anomaly_verdict == "UNKNOWN"


def test_action_plan_unsupported_shape_yields_declarative_only_ready() -> None:
    surface = _surface(action_plan={"version": 1, "legs": []})
    assert surface.action_plan.consumption == "DECLARATIVE_ONLY"
    assert surface.action_plan.anomaly_verdict == "READY"


def test_action_plan_single_long_stock_yields_active_ready() -> None:
    surface = _surface(
        start_defaults=InstanceStartDefaults(strategy="deployment_validation"),
        action_plan={
            "on_enter": [
                {
                    "leg_id": "nvda_long",
                    "instrument": {"kind": "stock", "underlying": "NVDA"},
                    "position": "long",
                    "qty_ratio": 1,
                }
            ],
            "on_exit": [{"kind": "close_leg", "entry_leg_id": "nvda_long"}],
        }
    )
    assert surface.action_plan.consumption == "ACTIVE"
    assert surface.action_plan.anomaly_verdict == "READY"


def test_action_plan_stock_shape_non_consuming_strategy_stays_declarative() -> None:
    surface = _surface(
        start_defaults=InstanceStartDefaults(strategy="spy_ema_crossover"),
        action_plan={
            "on_enter": [
                {
                    "leg_id": "nvda_long",
                    "instrument": {"kind": "stock", "underlying": "NVDA"},
                    "position": "long",
                    "qty_ratio": 1,
                }
            ],
            "on_exit": [{"kind": "close_leg", "entry_leg_id": "nvda_long"}],
        },
    )
    assert surface.action_plan.consumption == "DECLARATIVE_ONLY"
    assert surface.action_plan.anomaly_verdict == "READY"


# ---------------------------------------------------------------------------
# configuration verdict + 5 named rules
# ---------------------------------------------------------------------------


def _start_defaults(**overrides) -> InstanceStartDefaults:
    base: dict = {
        "strategy": "spy_ema",
        "readonly": False,
        "hydrate_policy": "optional",
        "max_orders_per_day": 50,
        "ibkr_host": "127.0.0.1",
    }
    base.update(overrides)
    return InstanceStartDefaults(**base)  # type: ignore[arg-type]


def _sizing(**overrides) -> InstanceSizing:
    base: dict = {
        "policy": {"kind": "fixed_shares", "value": 10},
        "preset": "explicit",
        "governed_by": "live_config",
        "sizing_provenance": "live_override",
        "per_trade_audit": [],
    }
    base.update(overrides)
    return InstanceSizing(**base)  # type: ignore[arg-type]


def test_configuration_nothing_deployed_is_unknown() -> None:
    surface = _surface(
        start_defaults=None,
        sizing=None,
        instance_broker_self_consistent=None,
    )
    assert surface.configuration.verdict == "UNKNOWN"
    assert surface.configuration.reason_codes == []


def test_configuration_all_rules_pass_is_ready() -> None:
    surface = _surface(
        start_defaults=_start_defaults(),
        sizing=_sizing(),
        instance_broker_self_consistent=True,
    )
    assert surface.configuration.verdict == "READY"
    assert surface.configuration.reason_codes == []


@pytest.mark.parametrize(
    ("start_kwargs", "sizing_kwargs", "self_consistent", "expected_codes"),
    [
        ({"strategy": ""}, {}, True, {"STRATEGY_KEY_MISSING"}),
        ({"strategy": "   "}, {}, True, {"STRATEGY_KEY_MISSING"}),
        ({"max_orders_per_day": 0}, {}, True, {"MAX_ORDERS_CAP_UNSET"}),
        ({"max_orders_per_day": -1}, {}, True, {"MAX_ORDERS_CAP_UNSET"}),
        ({}, {"policy": None}, True, {"SIZING_PRESET_MISSING"}),
        ({}, {}, False, {"INSTANCE_BROKER_SELF_INCONSISTENT"}),
    ],
)
def test_configuration_individual_rules_flag_their_codes(
    start_kwargs, sizing_kwargs, self_consistent, expected_codes
) -> None:
    surface = _surface(
        start_defaults=_start_defaults(**start_kwargs),
        sizing=_sizing(**sizing_kwargs),
        instance_broker_self_consistent=self_consistent,
    )
    assert surface.configuration.verdict == "ATTENTION"
    assert expected_codes.issubset(set(surface.configuration.reason_codes))


def test_configuration_sizing_entirely_missing_flags_both_sizing_codes() -> None:
    surface = _surface(
        start_defaults=_start_defaults(),
        sizing=None,
        instance_broker_self_consistent=True,
    )
    assert surface.configuration.verdict == "ATTENTION"
    assert {"SIZING_PRESET_MISSING", "SIZING_PROVENANCE_MISSING"}.issubset(set(surface.configuration.reason_codes))


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------


def test_resume_pause_under_clean_guards_and_paused_intent() -> None:
    # PRD #616 — Resume/Pause now consult the shared ``ResumeGuardState``
    # resolver AND the intent-state pair rules.  Under the
    # nothing-ever-deployed default (empty guard state, no desired
    # state sidecar), Resume is refused with ``ALREADY_RUNNING``
    # because absence is the effective-RUNNING default; Pause is
    # permitted.
    for binding in (None, _LIVE):
        surface = _surface(live_binding=binding, desired_state=_desired("PAUSED"))
        assert surface.actions.resume.enabled is True
        assert surface.actions.resume.disabled_reason_code is None
        assert surface.actions.resume.gate_results[0].gate_id == "action.resume"
        assert surface.actions.resume.gate_results[0].status == "pass"
        assert surface.actions.resume.gate_results[0].evidence_at_ms == _NOW_MS
        assert surface.actions.pause.enabled is False
        assert surface.actions.pause.disabled_reason_code == "ALREADY_PAUSED"
        assert surface.actions.pause.gate_results[0].gate_id == "action.pause"
        assert surface.actions.pause.gate_results[0].status == "block"
        assert surface.actions.pause.gate_results[0].operator_reason == "ALREADY_PAUSED"
        assert surface.actions.pause.gate_results[0].operator_next_step == "ALREADY_PAUSED"
        assert surface.actions.pause.gate_results[0].evidence_at_ms == _NOW_MS


def test_account_freeze_blocks_start_and_resume() -> None:
    freeze = AccountFreezeEvidence(
        account_id="DU123456",
        reason="watchdog.flatten_failed",
        source="watchdog_halt_executor",
        recorded_at_ms=_NOW_MS,
        operator_next_step="CHECK_IBKR",
    )

    surface = _surface(
        process=_IDLE_PROC,
        desired_state=_desired("PAUSED"),
        start_run_id=_START_RUN,
        start_defaults=_defaults(),
        account_freeze=freeze,
    )

    assert surface.host_process.start_capability.enabled is False
    assert surface.host_process.start_capability.disabled_reason_code == "ACCOUNT_FROZEN"
    start_gate = surface.host_process.start_capability.gate_results[0]
    assert start_gate.gate_id == "account.unresolved_exposure"
    assert start_gate.status == "freeze"

    assert surface.actions.resume.enabled is False
    assert surface.actions.resume.disabled_reason_code == "ACCOUNT_FROZEN"
    resume_gate = surface.actions.resume.gate_results[0]
    assert resume_gate.gate_id == "account.unresolved_exposure"
    assert resume_gate.status == "freeze"
    assert resume_gate.operator_reason == "watchdog.flatten_failed"


def test_resume_pause_effect_discriminator_flips_with_binding_and_state() -> None:
    no_binding = _surface(process=_IDLE_PROC, live_binding=None, desired_state=_desired("PAUSED"))
    assert no_binding.actions.resume.effect == "DURABLE_ONLY"
    assert no_binding.actions.pause.effect == "DURABLE_ONLY"
    bound = _surface(process=_PROC, live_binding=_LIVE, desired_state=_desired("PAUSED"))
    assert bound.actions.resume.effect == "LIVE_ACTUATION"
    assert bound.actions.pause.effect == "LIVE_ACTUATION"
    bound_idle = _surface(process=_IDLE_PROC, live_binding=_LIVE, desired_state=_desired("PAUSED"))
    assert bound_idle.actions.resume.effect == "DURABLE_ONLY"


def test_actions_stop_present_with_intent_state_rules() -> None:
    # PRD #616 — ``actions.stop`` is now on the operator surface.
    surface = _surface(desired_state=_desired("PAUSED"))
    assert surface.actions.stop.enabled is True

    stopped_surface = _surface(desired_state=_desired("STOPPED"))
    assert stopped_surface.actions.stop.enabled is False
    assert stopped_surface.actions.stop.disabled_reason_code == "ALREADY_STOPPED"


def test_flatten_and_pause_requires_live_binding() -> None:
    no_binding = _surface(live_binding=None, broker=_broker(owned_positions={"SPY": 1}))
    assert no_binding.actions.flatten_and_pause.enabled is False
    assert no_binding.actions.flatten_and_pause.disabled_reason_code == "NO_LIVE_BINDING"
    bound = _surface(live_binding=_LIVE, broker=_broker(owned_positions={"SPY": 1}))
    assert bound.actions.flatten_and_pause.enabled is True
    assert bound.actions.flatten_and_pause.effect == "LIVE_ACTUATION"


def test_flatten_and_pause_disabled_when_no_owned_positions() -> None:
    surface = _surface(live_binding=_LIVE, broker=_broker(owned_positions={}))
    assert surface.actions.flatten_and_pause.enabled is False
    assert surface.actions.flatten_and_pause.disabled_reason_code == "NO_OWNED_POSITIONS"


def test_mark_poisoned_rejects_without_binding() -> None:
    surface = _surface(live_binding=None)
    assert surface.actions.mark_poisoned.enabled is False
    assert surface.actions.mark_poisoned.disabled_reason_code == "NO_LIVE_BINDING"


def test_mark_poisoned_rejects_when_already_poisoned() -> None:
    surface = _surface(live_binding=_LIVE, poisoned=True)
    assert surface.actions.mark_poisoned.enabled is False
    assert surface.actions.mark_poisoned.disabled_reason_code == "ALREADY_POISONED"


def test_mark_poisoned_enabled_when_bound_and_not_poisoned() -> None:
    surface = _surface(live_binding=_LIVE, poisoned=False)
    assert surface.actions.mark_poisoned.enabled is True
    assert surface.actions.mark_poisoned.disabled_reason_code is None


# ---------------------------------------------------------------------------
# trading_session
# ---------------------------------------------------------------------------


def _ny_ms(year, month, day, hour, minute, second=0) -> int:
    """ms-since-epoch for the given America/New_York wall clock."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    dt = datetime(year, month, day, hour, minute, second, tzinfo=ny)
    return int(dt.timestamp() * 1000)


# Tuesday 2026-06-23 is an arbitrary RTH weekday.
_RTH_MID = _ny_ms(2026, 6, 23, 12, 0)  # 12:00 ET Tue
_PRE_MARKET = _ny_ms(2026, 6, 23, 6, 0)  # 06:00 ET Tue
_RTH_OPEN_EDGE = _ny_ms(2026, 6, 23, 9, 30)  # 09:30 ET Tue
_RTH_CLOSE_EDGE = _ny_ms(2026, 6, 23, 16, 0)  # 16:00 ET Tue
_POST_MARKET = _ny_ms(2026, 6, 23, 18, 0)  # 18:00 ET Tue
_OVERNIGHT = _ny_ms(2026, 6, 23, 2, 0)  # 02:00 ET Tue
_SATURDAY = _ny_ms(2026, 6, 27, 12, 0)  # noon Sat


@pytest.mark.parametrize(
    ("now_ms", "expected_phase", "expected_permits"),
    [
        (_OVERNIGHT, "CLOSED", False),
        (_PRE_MARKET, "PRE", False),
        (_RTH_OPEN_EDGE, "RTH", True),
        (_RTH_MID, "RTH", True),
        (_RTH_CLOSE_EDGE, "POST", False),  # at exactly 16:00 RTH has ended
        (_POST_MARKET, "POST", False),
        (_SATURDAY, "CLOSED", False),
    ],
)
def test_trading_session_phase_and_permission(now_ms, expected_phase, expected_permits) -> None:
    surface = _surface(now_ms=now_ms)
    assert surface.trading_session.phase == expected_phase
    assert surface.trading_session.permits_strategy_activity is expected_permits
    assert surface.trading_session.timezone == "America/New_York"
    assert surface.trading_session.as_of_ms == now_ms


def test_trading_session_next_transition_ms_overnight_points_to_pre_open() -> None:
    # PRD #616 — replace hard-coded None with the real next boundary.
    surface = _surface(now_ms=_OVERNIGHT)
    assert surface.trading_session.next_transition_ms == _ny_ms(2026, 6, 23, 4, 0)


def test_trading_session_next_transition_ms_pre_market_points_to_rth_open() -> None:
    surface = _surface(now_ms=_PRE_MARKET)
    assert surface.trading_session.next_transition_ms == _ny_ms(2026, 6, 23, 9, 30)


def test_trading_session_next_transition_ms_rth_points_to_close() -> None:
    surface = _surface(now_ms=_RTH_MID)
    assert surface.trading_session.next_transition_ms == _ny_ms(2026, 6, 23, 16, 0)


def test_trading_session_next_transition_ms_post_points_to_close_of_post() -> None:
    surface = _surface(now_ms=_POST_MARKET)
    assert surface.trading_session.next_transition_ms == _ny_ms(2026, 6, 23, 20, 0)


def test_trading_session_next_transition_ms_weekend_points_to_monday_open() -> None:
    # Saturday noon → next transition is Monday 04:00 NY.
    surface = _surface(now_ms=_SATURDAY)
    expected = _ny_ms(2026, 6, 29, 4, 0)
    assert surface.trading_session.next_transition_ms == expected


def test_trading_session_next_transition_ms_after_post_points_to_next_day() -> None:
    # 21:00 Tue ET → CLOSED, next boundary is Wed 04:00 ET.
    now = _ny_ms(2026, 6, 23, 21, 0)
    surface = _surface(now_ms=now)
    assert surface.trading_session.phase == "CLOSED"
    assert surface.trading_session.next_transition_ms == _ny_ms(2026, 6, 24, 4, 0)


# ---------------------------------------------------------------------------
# readiness_gates — OperatorGate projection
# ---------------------------------------------------------------------------


def test_readiness_gates_empty_when_no_readiness() -> None:
    assert _surface(readiness=None).readiness_gates == []


def test_readiness_gates_passing_gate_has_no_action_but_documented_unavailable_reason() -> None:
    surface = _surface(
        readiness=_readiness(
            as_of_ms=_NOW_MS,
            gates=[
                ReadinessGate(
                    name="broker_connection",
                    status="pass",
                    severity="hard",
                    detail="connected",
                )
            ],
        )
    )
    assert len(surface.readiness_gates) == 1
    gate = surface.readiness_gates[0]
    assert gate.status == "pass"
    assert gate.suggested_action is None
    assert gate.suggested_action_unavailable_reason == "GATE_PASSING"
    assert gate.gate_result.gate_id == "broker_connection"
    assert gate.gate_result.status == "pass"
    assert gate.gate_result.source == "engine"
    assert gate.gate_result.evidence_at_ms == _NOW_MS
    assert gate.gate_result.operator_reason == "connected"
    assert gate.gate_result.operator_next_step == "GATE_PASSING"


def test_readiness_gates_failing_gate_has_authored_action_or_unavailable_reason() -> None:
    # PRD #616 mandate: every non-passing gate either ships an action
    # OR ships ``null`` + a documented unavailable reason.
    failing_gates = [
        ReadinessGate(name=name, status="fail", severity="hard", detail="x")
        for name in (
            "broker_connection",
            "poison_sentinel",
            "fleet_contamination",
            "daily_order_cap",
            "warmup",
            "calendar",
            "session",
            "instrument_surface",
            "indicator_state_hydration",
            "spec_signature",
            "intent_wal_clean",
            "positions_self_consistent",
            "halt_clear",
            "totally_invented_gate",
        )
    ]
    surface = _surface(readiness=_readiness(gates=failing_gates))
    assert len(surface.readiness_gates) == len(failing_gates)
    for projected in surface.readiness_gates:
        if projected.suggested_action is None:
            assert projected.suggested_action_unavailable_reason is not None, projected.name
            assert projected.suggested_action_unavailable_reason != ""
        else:
            assert projected.suggested_action_unavailable_reason is None, projected.name


def test_readiness_gates_unknown_gate_name_surfaces_unavailable_reason() -> None:
    # An unknown gate name fails closed visibly — the cockpit shows
    # the raw name and the unavailable reason rather than guessing a
    # remediation.
    surface = _surface(
        readiness=_readiness(
            gates=[
                ReadinessGate(
                    name="totally_invented_gate",
                    status="fail",
                    severity="hard",
                    detail="",
                )
            ]
        )
    )
    g = surface.readiness_gates[0]
    assert g.suggested_action is None
    assert g.suggested_action_unavailable_reason == "UNKNOWN_GATE_NAME"


def test_readiness_gates_preserves_engine_order() -> None:
    names = ["calendar", "broker_connection", "warmup"]
    surface = _surface(
        readiness=_readiness(gates=[ReadinessGate(name=n, status="pass", severity="hard", detail="") for n in names])
    )
    assert [g.name for g in surface.readiness_gates] == names


# ---------------------------------------------------------------------------
# reason-code vocabulary closure
# ---------------------------------------------------------------------------


def test_reason_code_vocabulary_excludes_removed_codes() -> None:
    # PRD #616 — these legacy codes are removed from the closed
    # vocabulary in favour of the structured ADR-0011-aligned codes.
    assert "BUSY_VERB_IN_FLIGHT" not in REASON_CODES
    assert "NOT_RUNNING" not in REASON_CODES
    assert "SAFETY_BLOCK_HALT" not in REASON_CODES
    assert "RECONCILE_NOT_WIRED" not in REASON_CODES


def test_reason_code_vocabulary_lists_documented_codes() -> None:
    # PRD #616 — the closed vocabulary is the union of the legacy
    # live-binding codes plus every ResumeGuardState code; PRD #619-C5
    # added OUTCOME_UNKNOWN and #619-D added the four
    # MUTATION_UNRESOLVED_* matrix codes.
    documented = {
        "NO_LIVE_BINDING",
        "NO_OWNED_POSITIONS",
        "ALREADY_POISONED",
        "ALREADY_STOPPED",
        "POSTURE_DEMOTED",
        "OUTCOME_UNKNOWN",
        # ResumeGuardState (PRD #616) closed vocabulary.
        "BROKER_SAFETY_UNSAFE",
        "BROKER_SAFETY_UNKNOWN",
        "RECONCILIATION_FAILED",
        "RECONCILIATION_STALE",
        "RECONCILIATION_NOT_AVAILABLE",
        "RECONCILIATION_UNKNOWN",
        "UNRESOLVED_UNCERTAIN_INTENT",
        "UNCERTAIN_INTENT_STATE_UNKNOWN",
        "ALREADY_RUNNING",
        "ALREADY_PAUSED",
        "STOPPED_REQUIRES_REDEPLOY",
        "REDEPLOY_REQUIRED",
        # PRD #619-D action-conflict matrix codes.
        "MUTATION_UNRESOLVED_START",
        "MUTATION_UNRESOLVED_STOP",
        "MUTATION_UNRESOLVED_FLATTEN",
        "MUTATION_UNRESOLVED_RESUME",
    }
    assert documented.issubset(REASON_CODES)


# ---------------------------------------------------------------------------
# control_plane — PRD #619-C3
# ---------------------------------------------------------------------------


def _conn_state(
    kind: str,
    *,
    attempt: int = 0,
    last_transition_ms: int = _NOW_MS,
    last_success_ms: int | None = _NOW_MS,
    daemon_boot_id: str | None = "boot-A",
):
    from app.engine.live.daemon_connectivity_monitor import DaemonConnectivityState

    return DaemonConnectivityState(
        kind=kind,
        attempt=attempt,
        last_transition_ms=last_transition_ms,
        last_success_ms=last_success_ms,
        observed_daemon_boot_id=daemon_boot_id,
    )


def test_control_plane_none_when_no_monitor_installed() -> None:
    surface = _surface()

    assert surface.control_plane is None


def test_control_plane_connected_has_no_notice_or_runbook() -> None:
    surface = _surface(control_plane_state=_conn_state("CONNECTED"))

    assert surface.control_plane is not None
    assert surface.control_plane.state == "CONNECTED"
    assert surface.control_plane.notice is None
    assert surface.control_plane.runbook_slug is None
    assert surface.control_plane.daemon_boot_id == "boot-A"


@pytest.mark.parametrize(
    ("kind", "expected_runbook"),
    [
        ("RETRYING", "daemon-retrying"),
        ("UNREACHABLE", "daemon-unreachable"),
        ("AUTH_FAILED", "daemon-auth-failed"),
        ("PROTOCOL_ERROR", "daemon-protocol-error"),
        ("INCOMPATIBLE_CONTRACT", "daemon-incompatible-contract"),
    ],
)
def test_control_plane_unhealthy_kinds_carry_notice_and_runbook(kind: str, expected_runbook: str) -> None:
    surface = _surface(control_plane_state=_conn_state(kind, attempt=2))

    assert surface.control_plane is not None
    assert surface.control_plane.state == kind
    assert surface.control_plane.notice is not None
    assert isinstance(surface.control_plane.notice, str)
    assert surface.control_plane.runbook_slug == expected_runbook


def test_control_plane_forwards_monitor_observability_fields() -> None:
    state = _conn_state(
        "RETRYING",
        attempt=3,
        last_transition_ms=_NOW_MS + 500,
        last_success_ms=_NOW_MS - 1_000,
        daemon_boot_id="boot-deadbeef",
    )

    surface = _surface(control_plane_state=state)

    cp = surface.control_plane
    assert cp is not None
    assert cp.attempt == 3
    assert cp.last_transition_ms == _NOW_MS + 500
    assert cp.last_success_ms == _NOW_MS - 1_000
    assert cp.daemon_boot_id == "boot-deadbeef"


def test_control_plane_carries_initial_no_success_state() -> None:
    # Monitor freshly started, no probe yet: kind=RETRYING, attempt=0,
    # last_success_ms=None.
    state = _conn_state(
        "RETRYING",
        attempt=0,
        last_transition_ms=_NOW_MS,
        last_success_ms=None,
        daemon_boot_id=None,
    )

    surface = _surface(control_plane_state=state)

    cp = surface.control_plane
    assert cp is not None
    assert cp.state == "RETRYING"
    assert cp.last_success_ms is None
    assert cp.daemon_boot_id is None
    assert cp.notice is not None  # retrying-class notice still authored


def test_evaluator_only_emits_codes_in_the_documented_vocabulary() -> None:
    emitted: set[str] = set()
    for action in ("resume", "pause", "stop", "flatten_and_pause", "mark_poisoned"):
        for binding in (None, _LIVE):
            for poisoned in (False, True):
                for owned_empty in (True, False):
                    for intent_state in (None, "RUNNING", "PAUSED", "STOPPED"):
                        cap = evaluate_action(
                            action,  # type: ignore[arg-type]
                            process=_PROC,
                            live_binding=binding,
                            poisoned=poisoned,
                            owned_positions_empty=owned_empty,
                            desired_state=_desired(intent_state),
                        )
                        if cap.disabled_reason_code is not None:
                            emitted.add(cap.disabled_reason_code)
                        emitted.update(cap.disabled_reasons)
    assert emitted.issubset(REASON_CODES), f"orphan codes emitted: {emitted - REASON_CODES}"


# ---------------------------------------------------------------------------
# Cold-start reconciliation projection (ADR-0008 §5 / PR 1)
# ---------------------------------------------------------------------------


def _make_receipt(
    *,
    status: str,
    outcome: str | None = None,
    failure_reason: str | None = None,
    sidecar_wal_seq: int = 0,
    broker_observed_at_ms: int | None = 1000,
    last_reconcile_ms: int | None = 1000,
    run_id: str = "run-1",
    namespace: str = "learn-ai/sid/v1",
    adopted_intent_ids: tuple[str, ...] = (),
):
    from app.schemas.live_runs import ReconciliationReceipt

    return ReconciliationReceipt(
        status=status,  # type: ignore[arg-type]
        outcome=outcome,  # type: ignore[arg-type]
        run_id=run_id,
        strategy_instance_id="sid",
        namespace=namespace,
        started_at_ms=999,
        completed_at_ms=last_reconcile_ms,
        last_reconcile_ms=last_reconcile_ms,
        sidecar_wal_seq=sidecar_wal_seq,
        broker_observed_at_ms=broker_observed_at_ms,
        adopted_intent_ids=adopted_intent_ids,
        failure_reason=failure_reason,
    )


def _project(receipt=None, **kwargs):
    from app.services.operator_surface import _project_reconciliation

    defaults: dict = {
        "current_wal_seq": None,
        "current_run_id": None,
        "current_namespace": None,
        "latest_broker_event_ms": None,
        "latest_mutation_ms": None,
        "ttl_ms": None,
        "now_ms": 2000,
    }
    defaults.update(kwargs)
    return _project_reconciliation(receipt, **defaults)


def test_project_reconciliation_missing_receipt_is_not_available() -> None:
    proj = _project(None)
    assert proj.state == "NOT_AVAILABLE"


def test_project_reconciliation_in_progress() -> None:
    proj = _project(_make_receipt(status="in_progress"))
    assert proj.state == "IN_PROGRESS"


def test_project_reconciliation_failed_carries_reason() -> None:
    proj = _project(_make_receipt(status="failed", failure_reason="broker_probe_failed"))
    assert proj.state == "FAILED"
    assert proj.failure_reason == "broker_probe_failed"
    assert proj.sidecar_wal_seq == 0
    assert proj.broker_observed_at_ms == 1000


def test_project_reconciliation_passed_clean() -> None:
    proj = _project(_make_receipt(status="passed", outcome="clean"))
    assert proj.state == "CLEAN"


def test_project_reconciliation_passed_adopted_surfaces_intent_ids() -> None:
    proj = _project(_make_receipt(status="passed", outcome="adopted", adopted_intent_ids=("iid-1",)))
    assert proj.state == "ADOPTED"
    assert proj.adopted_intent_ids == ("iid-1",)


def test_project_reconciliation_stale_when_wal_advances() -> None:
    proj = _project(
        _make_receipt(status="passed", outcome="clean", sidecar_wal_seq=5),
        current_wal_seq=6,
    )
    assert proj.state == "STALE"


def test_project_reconciliation_stale_when_run_id_changes() -> None:
    proj = _project(
        _make_receipt(status="passed", outcome="clean", run_id="run-old"),
        current_run_id="run-new",
    )
    assert proj.state == "STALE"


def test_project_reconciliation_stale_when_namespace_changes() -> None:
    proj = _project(
        _make_receipt(status="passed", outcome="clean", namespace="learn-ai/sid/v1"),
        current_namespace="learn-ai/sid/v2",
    )
    assert proj.state == "STALE"


def test_project_reconciliation_stale_when_broker_event_after_observed() -> None:
    proj = _project(
        _make_receipt(status="passed", outcome="clean", broker_observed_at_ms=1000),
        latest_broker_event_ms=2000,
    )
    assert proj.state == "STALE"


def test_project_reconciliation_stale_when_mutation_after_observed() -> None:
    proj = _project(
        _make_receipt(status="passed", outcome="clean", broker_observed_at_ms=1000),
        latest_mutation_ms=2000,
    )
    assert proj.state == "STALE"


def test_project_reconciliation_stale_when_ttl_exceeded() -> None:
    proj = _project(
        _make_receipt(status="passed", outcome="clean", last_reconcile_ms=1000),
        ttl_ms=500,
        now_ms=2000,
    )
    assert proj.state == "STALE"


def test_project_reconciliation_fresh_passes_within_ttl() -> None:
    proj = _project(
        _make_receipt(status="passed", outcome="clean", last_reconcile_ms=1900),
        ttl_ms=500,
        now_ms=2000,
    )
    assert proj.state == "CLEAN"


def test_project_reconciliation_matching_inputs_keep_clean() -> None:
    """All freshness inputs aligned with the receipt → CLEAN remains CLEAN."""
    proj = _project(
        _make_receipt(
            status="passed",
            outcome="clean",
            sidecar_wal_seq=10,
            run_id="run-1",
            namespace="learn-ai/sid/v1",
            broker_observed_at_ms=1500,
            last_reconcile_ms=1500,
        ),
        current_wal_seq=10,
        current_run_id="run-1",
        current_namespace="learn-ai/sid/v1",
        latest_broker_event_ms=1500,
        latest_mutation_ms=1500,
        ttl_ms=10_000,
        now_ms=2000,
    )
    assert proj.state == "CLEAN"
    assert proj.sidecar_wal_seq == 10
    assert proj.broker_observed_at_ms == 1500


def test_compute_operator_surface_default_reconciliation_is_not_available() -> None:
    """The aggregate compose function defaults to NOT_AVAILABLE when no
    receipt + freshness inputs are passed (callsite-additive contract)."""
    surface = compute_operator_surface(
        process=_PROC,
        now_ms=_NOW_MS,
    )
    assert surface.reconciliation is not None
    assert surface.reconciliation.state == "NOT_AVAILABLE"
