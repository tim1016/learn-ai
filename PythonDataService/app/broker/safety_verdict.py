"""Phase 7A / VCR-0010 / ADR 0011 — broker safety verdict.

The cockpit hero historically rendered a hardcoded "Paper trading mode -
no real money at risk" string with no consultation of the actual broker
mode. Per ADR 0011 §1-3, the server now publishes a structured
``BrokerSafetyVerdict`` on the broker-status payload; the Frontend renders
its ``final_verdict`` directly without recomputing.

The derivation is fail-closed:

- ``paper-only`` iff every identity gate positively confirms paper.
- ``unsafe`` iff any gate positively indicates live / non-paper risk.
- ``unknown`` otherwise.

ADR-0011 amendment (PRD #619-A): identity (``configured_mode``, port,
account prefix) and submission capability are independent facts.
``readonly_flag`` is still carried on the wire as a diagnostic field but
no longer participates in the ``paper-only`` derivation — an executing
paper bot runs with ``readonly=false`` and must still be able to reach
``paper-only`` so guarded Resume can compose its four gates. Capability
authority lives in durable child/run evidence (declared ``submit_mode`` +
the actual ``readonly`` setting used to construct the child), not here.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class BrokerSafetyVerdict(BaseModel):
    """ADR 0011 §1 — the broker safety verdict shape.

    Rides on the existing broker-status / readiness payload — no new
    transport, no connect-time cache. The Frontend MUST NOT derive
    ``final_verdict`` from the raw gate fields; it renders the
    server-derived ``final_verdict`` directly and may show the
    per-gate fields as expandable detail.
    """

    model_config = ConfigDict(frozen=True)

    configured_mode: Literal["paper", "live", "unknown"]
    readonly_flag: bool | None
    port_class: Literal["paper_port", "live_port", "unknown"]
    connected_account_prefix: Literal["DU", "non_DU"] | None
    final_verdict: Literal["paper-only", "unsafe", "unknown"]
    failing_gates: list[str]
    unknown_gates: list[str]


# ADR 0011 §3 — paper-port allow-list. IBKR Gateway / TWS uses 7497 / 4002
# for paper; 7496 / 4001 for live. The allow-list is conservative; an
# unrecognized port is classified ``unknown`` (degrades to ``unknown``
# verdict), never ``paper_port``.
_PAPER_PORTS: set[int] = {7497, 4002}
_LIVE_PORTS: set[int] = {7496, 4001}


def classify_port(port: int | None) -> Literal["paper_port", "live_port", "unknown"]:
    """Map the configured Gateway port to its safety class. Unrecognized
    ports are ``unknown`` so a misconfiguration cannot silently degrade
    a live verdict into a paper one."""
    if port is None:
        return "unknown"
    if port in _PAPER_PORTS:
        return "paper_port"
    if port in _LIVE_PORTS:
        return "live_port"
    return "unknown"


def classify_account_prefix(
    account_id: str | None,
) -> Literal["DU", "non_DU"] | None:
    """``DU`` if the account looks like an IBKR paper account; ``non_DU``
    if it positively does not; ``None`` if there is no signal yet."""
    if not account_id:
        return None
    return "DU" if account_id.upper().startswith("DU") else "non_DU"


def derive_broker_safety_verdict(
    *,
    configured_mode: Literal["paper", "live"] | None,
    readonly_flag: bool | None,
    port: int | None,
    connected_account: str | None,
) -> BrokerSafetyVerdict:
    """Pure derivation — the same inputs always produce the same verdict.

    Every gate contributes to the three lists:

    - ``failing_gates``: positively unsafe signals (mode/port/account).
    - ``unknown_gates``: signals that could not be confirmed.
    - The ``final_verdict`` is the resolution of those two lists:
      - any failing gate → ``unsafe``.
      - else any unknown gate → ``unknown``.
      - else ``paper-only``.
    """
    failing: list[str] = []
    unknown: list[str] = []

    mode_resolved: Literal["paper", "live", "unknown"] = (
        configured_mode if configured_mode in ("paper", "live") else "unknown"
    )
    if mode_resolved == "live":
        failing.append("configured_mode")
    elif mode_resolved == "unknown":
        unknown.append("configured_mode")

    # ADR-0011 amendment (PRD #619-A): readonly_flag is carried verbatim on
    # the wire as diagnostic detail but no longer contributes to the
    # identity derivation. Submission capability is a separate fact derived
    # from durable child/run evidence; see ``operator_surface`` and the
    # 619-A Resume composition.

    port_class = classify_port(port)
    if port_class == "live_port":
        failing.append("port_class")
    elif port_class == "unknown":
        unknown.append("port_class")

    account_prefix = classify_account_prefix(connected_account)
    if account_prefix == "non_DU":
        failing.append("connected_account_prefix")
    elif account_prefix is None:
        unknown.append("connected_account_prefix")

    if failing:
        final: Literal["paper-only", "unsafe", "unknown"] = "unsafe"
    elif unknown:
        final = "unknown"
    else:
        final = "paper-only"

    return BrokerSafetyVerdict(
        configured_mode=mode_resolved,
        readonly_flag=readonly_flag,
        port_class=port_class,
        connected_account_prefix=account_prefix,
        final_verdict=final,
        failing_gates=failing,
        unknown_gates=unknown,
    )
