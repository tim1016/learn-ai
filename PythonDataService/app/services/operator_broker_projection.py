"""Broker proof projection for the operator surface."""

from __future__ import annotations

from typing import Literal

from app.schemas.live_runs import (
    BrokerConnectionState,
    BrokerSafetyVerdictEnum,
    OperatorSurfaceBroker,
    OperatorSurfaceNamedCondition,
)

BrokerConnectionStateInput = Literal[
    "connected",
    "soft_lost",
    "subscriptions_stale",
    "degraded_data_farm",
    "reconnecting",
    "recovering",
    "hard_down",
    "disconnected",
    "disabled",
    "unknown",
]

_BROKER_FINAL_VERDICT_TO_SAFETY: dict[
    Literal["paper-only", "unsafe", "unknown"],
    BrokerSafetyVerdictEnum,
] = {
    "paper-only": "PAPER_ONLY",
    "unsafe": "UNSAFE",
    "unknown": "UNKNOWN",
}

_BROKER_DEGRADED_STATES: frozenset[BrokerConnectionStateInput] = frozenset(
    {
        "soft_lost",
        "subscriptions_stale",
        "degraded_data_farm",
        "reconnecting",
        "recovering",
    }
)
_BROKER_DISCONNECTED_STATES: frozenset[BrokerConnectionStateInput] = frozenset(
    {"disconnected", "disabled", "hard_down"}
)


def project_broker(
    safety_verdict_final: Literal["paper-only", "unsafe", "unknown"] | None,
    connection_state: BrokerConnectionStateInput | None,
    *,
    runtime_bound: bool = True,
) -> OperatorSurfaceBroker:
    """Project broker safety and connection evidence into the operator DTO."""

    if connection_state == "connected":
        connection: BrokerConnectionState = "CONNECTED"
    elif connection_state in _BROKER_DISCONNECTED_STATES:
        connection = "DISCONNECTED"
    elif connection_state in _BROKER_DEGRADED_STATES:
        connection = "DEGRADED"
    else:
        connection = "UNKNOWN"

    safety_verdict: BrokerSafetyVerdictEnum = "UNKNOWN"
    if safety_verdict_final is not None:
        safety_verdict = _BROKER_FINAL_VERDICT_TO_SAFETY[safety_verdict_final]

    return OperatorSurfaceBroker(
        safety_verdict=safety_verdict,
        connection=connection,
        connection_condition=_broker_connection_condition(
            connection_state,
            runtime_bound=runtime_bound,
        ),
    )


def _broker_connection_condition(
    connection_state: BrokerConnectionStateInput | None,
    *,
    runtime_bound: bool,
) -> OperatorSurfaceNamedCondition:
    match connection_state:
        case "connected":
            return OperatorSurfaceNamedCondition(
                code="BROKER_CONNECTED",
                severity="ok",
                title="Broker session connected",
                summary="The runtime has fresh proof that the IBKR broker session is connected.",
            )
        case "soft_lost":
            return OperatorSurfaceNamedCondition(
                code="BROKER_LINK_SOFT_LOST",
                severity="warning",
                title="Broker feed lost",
                summary="The broker link reported a soft loss; recovery is in progress.",
                remediation="Wait for recovery or reconnect the broker session before submitting orders.",
            )
        case "subscriptions_stale":
            return OperatorSurfaceNamedCondition(
                code="BROKER_SUBSCRIPTIONS_STALE",
                severity="warning",
                title="Broker subscriptions stale",
                summary="The broker session is connected, but market-data subscriptions need refresh after recovery.",
                remediation="Let the runtime resubscribe streams, then refresh broker evidence.",
            )
        case "degraded_data_farm":
            return OperatorSurfaceNamedCondition(
                code="BROKER_DATA_FARM_DEGRADED",
                severity="critical",
                title="IBKR data farm degraded",
                summary="The live broker data farm is degraded while this bot depends on broker market data.",
                remediation="Do not submit new orders until IBKR market-data evidence is healthy again.",
            )
        case "reconnecting":
            return OperatorSurfaceNamedCondition(
                code="BROKER_RECONNECTING",
                severity="warning",
                title="Broker reconnecting",
                summary="The runtime is reconnecting the broker session.",
                remediation="Wait for the reconnect to complete before submitting orders.",
            )
        case "recovering":
            return OperatorSurfaceNamedCondition(
                code="BROKER_RECOVERING",
                severity="warning",
                title="Broker recovering streams",
                summary="The broker link is back, but runtime stream recovery is still underway.",
                remediation="Wait for recovery probes and subscriptions to pass before submitting orders.",
            )
        case "hard_down":
            return OperatorSurfaceNamedCondition(
                code="BROKER_HARD_DOWN",
                severity="critical",
                title="Broker recovery exhausted",
                summary="Automatic broker reconnect attempts are exhausted.",
                remediation="Manually restore the IBKR Gateway/TWS connection before relying on this bot.",
            )
        case "disconnected":
            return OperatorSurfaceNamedCondition(
                code="BROKER_DISCONNECTED",
                severity="warning",
                title="Broker session disconnected",
                summary="The runtime cannot prove an active IBKR broker session.",
                remediation="Reconnect the broker session, then refresh broker evidence.",
            )
        case "disabled":
            return OperatorSurfaceNamedCondition(
                code="BROKER_DISABLED",
                severity="info",
                title="Broker session disabled",
                summary="The data-plane broker client is disabled for this process.",
            )
        case _:
            if not runtime_bound:
                return OperatorSurfaceNamedCondition(
                    code="BROKER_RUNTIME_UNBOUND",
                    severity="warning",
                    title="No live bot runtime is bound",
                    summary=(
                        "The global data-plane broker may be connected, but this bot has no "
                        "live runtime bound to publish per-bot broker proof."
                    ),
                    remediation=(
                        "Manually verify IBKR account state, then start or redeploy the bot "
                        "so a child runtime can record fresh broker evidence."
                    ),
                )
            return OperatorSurfaceNamedCondition(
                code="BROKER_CONNECTION_UNKNOWN",
                severity="warning",
                title="Broker connection unproven",
                summary=(
                    "A live bot runtime is bound, but its broker evidence is missing, stale, "
                    "or not specific enough to prove the per-bot broker session."
                ),
                remediation="Refresh the live runtime broker evidence or inspect the runtime freshness notices.",
            )
