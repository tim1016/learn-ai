"""Readiness vector — "can this strategy act on the next bar?" (ADR 0005).

Pure logic, no I/O. The live engine builds a ``live_readiness`` vector each tick
from its in-loop guard values and writes it to a sidecar; the status endpoint
transports it verbatim. For a dead instance the backend builds a labelled
``start_readiness`` from durable artifacts. Both share the verdict rules here so
the operator console shows exactly what the engine enforces — never a second,
drifting implementation of the gate logic.

Verdict rules:
  READY    = all hard gates pass, no material soft warnings
  BLOCKED  = at least one hard gate fails
  DEGRADED = hard gates pass, but a hard gate is unknown or a soft gate warns
  UNKNOWN  = no gates / no authoritative source
"""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas.bot_events import GateStep, GateStepResult, SourceAuthority

PASS = "pass"
FAIL = "fail"
UNKNOWN = "unknown"
HARD = "hard"
SOFT = "soft"

READY = "READY"
BLOCKED = "BLOCKED"
DEGRADED = "DEGRADED"
VERDICT_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ReadinessEmission:
    """One gate evaluation projected as current readiness plus raw gate-steps."""

    vector: dict
    gate_steps: tuple[GateStep, ...]


def gate(name: str, status: str, severity: str, detail: str) -> dict:
    return {"name": name, "status": status, "severity": severity, "detail": detail}


def _gate_result_status(status: str) -> str:
    if status == PASS:
        return "pass"
    if status == FAIL:
        return "block"
    if status == UNKNOWN:
        return "unknown"
    return "unknown"


def _step_result_from_readiness(*, status: str, severity: str) -> GateStepResult:
    if status == PASS:
        return GateStepResult.PASS
    if status == FAIL and severity == HARD:
        return GateStepResult.BLOCK
    return GateStepResult.SKIP


def _with_gate_results(gates: list[dict], *, source: str, as_of_ms: int) -> list[dict]:
    out: list[dict] = []
    for item in gates:
        if "gate_result" in item:
            out.append(item)
            continue
        detail = str(item.get("detail") or "")
        out.append(
            {
                **item,
                "gate_result": {
                    "gate_id": str(item.get("name") or ""),
                    "status": _gate_result_status(str(item.get("status") or UNKNOWN)),
                    "source": source,
                    "operator_reason": detail,
                    "operator_next_step": "GATE_PASSING" if item.get("status") == PASS else detail,
                    "evidence_at_ms": as_of_ms,
                },
            }
        )
    return out


def _gate_steps_from_gates(
    gates: list[dict],
    *,
    evaluation_id: str,
    readiness_kind: str,
    readiness_source: str,
    source_authority: SourceAuthority = SourceAuthority.ENGINE_LOOP,
) -> tuple[GateStep, ...]:
    steps: list[GateStep] = []
    for item in gates:
        gate_result = item.get("gate_result") or {}
        gate_id = str(gate_result.get("gate_id") or item.get("name") or "")
        status = str(item.get("status") or UNKNOWN)
        severity = str(item.get("severity") or HARD)
        detail = str(item.get("detail") or "")
        facts = {
            "readiness_kind": readiness_kind,
            "readiness_source": readiness_source,
            "readiness_status": status,
            "readiness_severity": severity,
            "detail": detail,
            "operator_reason": str(gate_result.get("operator_reason") or detail),
            "operator_next_step": str(gate_result.get("operator_next_step") or ""),
        }
        evidence_at_ms = gate_result.get("evidence_at_ms")
        if isinstance(evidence_at_ms, int):
            facts["evidence_at_ms"] = evidence_at_ms
        steps.append(
            GateStep(
                evaluation_id=evaluation_id,
                gate_id=gate_id,
                gate_result=_step_result_from_readiness(status=status, severity=severity),
                source_authority=source_authority,
                facts=facts,
            )
        )
    return tuple(steps)


def derive_verdict(gates: list[dict]) -> str:
    """Collapse the gate list into a single verdict per the ADR 0005 rules."""
    if not gates:
        return VERDICT_UNKNOWN
    hard = [g for g in gates if g["severity"] == HARD]
    if any(g["status"] == FAIL for g in hard):
        return BLOCKED
    if any(g["status"] == UNKNOWN for g in hard):
        return DEGRADED
    soft = [g for g in gates if g["severity"] == SOFT]
    if any(g["status"] in (FAIL, UNKNOWN) for g in soft):
        return DEGRADED
    return READY


def _summarize(gates: list[dict], verdict: str) -> str:
    if verdict == READY:
        return "Ready to act on the next bar; all hard gates pass."
    blocking = next((g for g in gates if g["severity"] == HARD and g["status"] == FAIL), None)
    if blocking is not None:
        return f"Blocked: {blocking['name']} — {blocking['detail']}."
    warning = next(
        (g for g in gates if g["status"] in (FAIL, UNKNOWN)),
        None,
    )
    if warning is not None:
        return f"Degraded: {warning['name']} — {warning['detail']}."
    return "Readiness unknown."


def build_live_readiness(
    *,
    as_of_ms: int,
    paused: bool,
    broker_connected: bool,
    submit_mode: str,
    orders_used: int,
    orders_cap: int | None,
    in_session: bool,
    force_flat_active: bool,
    poisoned: bool,
    bar_source: str,
    expected_bar_source: str,
    account_registry_gate_result: dict | None = None,
    account_truth_gate_result: dict | None = None,
) -> dict:
    """Engine-authored live readiness from in-loop guard values."""
    vector, _gates = _build_live_readiness_projection(
        as_of_ms=as_of_ms,
        paused=paused,
        broker_connected=broker_connected,
        submit_mode=submit_mode,
        orders_used=orders_used,
        orders_cap=orders_cap,
        in_session=in_session,
        force_flat_active=force_flat_active,
        poisoned=poisoned,
        bar_source=bar_source,
        expected_bar_source=expected_bar_source,
        account_registry_gate_result=account_registry_gate_result,
        account_truth_gate_result=account_truth_gate_result,
    )
    return vector


def build_live_readiness_emission(
    *,
    as_of_ms: int,
    paused: bool,
    broker_connected: bool,
    submit_mode: str,
    orders_used: int,
    orders_cap: int | None,
    in_session: bool,
    force_flat_active: bool,
    poisoned: bool,
    bar_source: str,
    expected_bar_source: str,
    evaluation_id: str,
    account_registry_gate_result: dict | None = None,
    account_truth_gate_result: dict | None = None,
) -> ReadinessEmission:
    """Build current readiness and raw gate-steps from one gate payload."""
    vector, gates = _build_live_readiness_projection(
        as_of_ms=as_of_ms,
        paused=paused,
        broker_connected=broker_connected,
        submit_mode=submit_mode,
        orders_used=orders_used,
        orders_cap=orders_cap,
        in_session=in_session,
        force_flat_active=force_flat_active,
        poisoned=poisoned,
        bar_source=bar_source,
        expected_bar_source=expected_bar_source,
        account_registry_gate_result=account_registry_gate_result,
        account_truth_gate_result=account_truth_gate_result,
    )
    return ReadinessEmission(
        vector=vector,
        gate_steps=_gate_steps_from_gates(
            gates,
            evaluation_id=evaluation_id,
            readiness_kind="live_readiness",
            readiness_source="engine",
        ),
    )


def _build_live_readiness_projection(
    *,
    as_of_ms: int,
    paused: bool,
    broker_connected: bool,
    submit_mode: str,
    orders_used: int,
    orders_cap: int | None,
    in_session: bool,
    force_flat_active: bool,
    poisoned: bool,
    bar_source: str,
    expected_bar_source: str,
    account_registry_gate_result: dict | None = None,
    account_truth_gate_result: dict | None = None,
) -> tuple[dict, list[dict]]:
    gates: list[dict] = [
        gate("desired_state", FAIL if paused else PASS, HARD, "PAUSED" if paused else "RUNNING"),
        gate(
            "broker_connection",
            PASS if broker_connected else FAIL,
            HARD,
            "connected" if broker_connected else "disconnected",
        ),
        gate("poison_sentinel", FAIL if poisoned else PASS, HARD, "poisoned" if poisoned else "clear"),
    ]
    if force_flat_active:
        gates.append(gate("session_window", FAIL, HARD, "past force-flat; flat-only"))
    elif not in_session:
        gates.append(gate("session_window", FAIL, HARD, "outside trading session"))
    else:
        gates.append(gate("session_window", PASS, HARD, "in session"))
    if orders_cap is not None:
        capped = orders_used >= orders_cap
        gates.append(gate("orders_cap", FAIL if capped else PASS, HARD, f"{orders_used} / {orders_cap} orders used"))
    gates.append(gate("submission_mode", PASS, SOFT, submit_mode))
    if account_registry_gate_result is not None:
        registry_status = str(account_registry_gate_result.get("status") or UNKNOWN)
        if registry_status == "pass":
            status = PASS
        elif registry_status == "unknown":
            status = UNKNOWN
        else:
            status = FAIL
        gates.append(
            {
                **gate(
                    "account_instance_registry",
                    status,
                    HARD,
                    str(account_registry_gate_result.get("operator_reason") or registry_status),
                ),
                "gate_result": account_registry_gate_result,
            }
        )
    if account_truth_gate_result is not None:
        truth_status = str(account_truth_gate_result.get("status") or UNKNOWN)
        truth_reason = str(account_truth_gate_result.get("operator_reason") or truth_status)
        # A running process is degraded as soon as broker truth goes away, while
        # the submit gate may still grant its bounded outage grace. Once that
        # grace expires, the same canonical gate becomes a hard block.
        if truth_status == "pass" and truth_reason == "BROKER_TRUTH_GRACE":
            status = UNKNOWN
        elif truth_status == "pass":
            status = PASS
        elif truth_status == "unknown":
            status = UNKNOWN
        else:
            status = FAIL
        gates.append(
            {
                **gate("account_broker_truth", status, HARD, truth_reason),
                "gate_result": account_truth_gate_result,
            }
        )
    if expected_bar_source and bar_source and bar_source != expected_bar_source:
        gates.append(gate("data_provenance", FAIL, SOFT, f"expected {expected_bar_source}; latest {bar_source}"))
    else:
        gates.append(gate("data_provenance", PASS, SOFT, bar_source or "n/a"))

    verdict = derive_verdict(gates)
    gates = _with_gate_results(gates, source="engine", as_of_ms=as_of_ms)
    # PRD #607 / Slice 1 (#608) — emit ``orders_used`` / ``orders_cap``
    # as structured top-level fields alongside the existing
    # ``orders_cap`` gate ``detail`` prose.  The cockpit's
    # ``operator_surface.daily_order_cap`` projection consumes the
    # structured fields; the gate prose stays for human readability.
    vector = {
        "kind": "live_readiness",
        "as_of_ms": as_of_ms,
        "source": "engine",
        "verdict": verdict,
        "summary": _summarize(gates, verdict),
        "gates": gates,
        "orders_used": orders_used,
        "orders_cap": orders_cap,
    }
    return vector, gates


def build_start_readiness(
    *,
    as_of_ms: int,
    desired_state: str | None,
    poisoned: bool,
    halted: bool,
    reconcile_passed: bool | None,
) -> dict:
    """Backend-derived start readiness for a dead instance (durable artifacts only).

    Answers "would this instance be allowed to start and act?" — labelled
    distinctly so it is never confused with engine-authored live readiness.
    """
    gates: list[dict] = []
    if desired_state == "STOPPED":
        gates.append(gate("desired_state", FAIL, HARD, "STOPPED — start refused until intent changes"))
    elif desired_state in (None, ""):
        gates.append(gate("desired_state", UNKNOWN, HARD, "no durable intent recorded"))
    else:
        gates.append(gate("desired_state", PASS, HARD, desired_state))
    gates.append(gate("poison_sentinel", FAIL if poisoned else PASS, HARD, "poisoned" if poisoned else "clear"))
    gates.append(gate("prior_day_halt", FAIL if halted else PASS, HARD, "halt sentinel set" if halted else "clear"))
    if reconcile_passed is None:
        gates.append(gate("latest_reconcile", UNKNOWN, SOFT, "no reconcile receipt"))
    else:
        gates.append(
            gate(
                "latest_reconcile", PASS if reconcile_passed else FAIL, SOFT, "passed" if reconcile_passed else "failed"
            )
        )

    verdict = derive_verdict(gates)
    gates = _with_gate_results(gates, source="backend_derived", as_of_ms=as_of_ms)
    summary = _summarize(gates, verdict)
    if verdict == READY:
        summary = "Start-ready: durable gates permit a start."
    return {
        "kind": "start_readiness",
        "as_of_ms": as_of_ms,
        "source": "backend_derived",
        "verdict": verdict,
        "summary": summary,
        "gates": gates,
        "live_readiness_available": False,
    }
