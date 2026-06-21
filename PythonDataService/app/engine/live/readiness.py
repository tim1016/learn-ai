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

PASS = "pass"
FAIL = "fail"
UNKNOWN = "unknown"
HARD = "hard"
SOFT = "soft"

READY = "READY"
BLOCKED = "BLOCKED"
DEGRADED = "DEGRADED"
VERDICT_UNKNOWN = "UNKNOWN"


def gate(name: str, status: str, severity: str, detail: str) -> dict:
    return {"name": name, "status": status, "severity": severity, "detail": detail}


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
) -> dict:
    """Engine-authored live readiness from in-loop guard values."""
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
        gates.append(
            gate("orders_cap", FAIL if capped else PASS, HARD, f"{orders_used} / {orders_cap} orders used")
        )
    gates.append(gate("submission_mode", PASS, SOFT, submit_mode))
    if expected_bar_source and bar_source and bar_source != expected_bar_source:
        gates.append(
            gate("data_provenance", FAIL, SOFT, f"expected {expected_bar_source}; latest {bar_source}")
        )
    else:
        gates.append(gate("data_provenance", PASS, SOFT, bar_source or "n/a"))

    verdict = derive_verdict(gates)
    # PRD #607 / Slice 1 (#608) — emit ``orders_used`` / ``orders_cap``
    # as structured top-level fields alongside the existing
    # ``orders_cap`` gate ``detail`` prose.  The cockpit's
    # ``operator_surface.daily_order_cap`` projection consumes the
    # structured fields; the gate prose stays for human readability.
    return {
        "kind": "live_readiness",
        "as_of_ms": as_of_ms,
        "source": "engine",
        "verdict": verdict,
        "summary": _summarize(gates, verdict),
        "gates": gates,
        "orders_used": orders_used,
        "orders_cap": orders_cap,
    }


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
            gate("latest_reconcile", PASS if reconcile_passed else FAIL, SOFT, "passed" if reconcile_passed else "failed")
        )

    verdict = derive_verdict(gates)
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
