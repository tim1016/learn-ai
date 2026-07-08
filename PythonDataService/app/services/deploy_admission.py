"""Deploy-start admission policy for live instances.

Routers supply transport inputs and disk evidence; this module decides whether
Deploy & start must be blocked for identity or exposure coherence.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.engine.live.config import stock_symbol_from_action_plan
from app.schemas.live_runs import (
    ExposureCoherenceFacts,
    InstanceBrokerView,
    LiveInstanceDeployRequest,
)
from app.services.operator_surface import (
    compose_exposure_coherence_facts,
    normalize_exposure_positions,
)


@dataclass(frozen=True)
class SymbolResolution:
    value: str
    source: str


@dataclass(frozen=True)
class DeployAdmissionBlock:
    status_code: int
    detail: dict[str, object]


def resolve_symbol_from_ledger(
    ledger: Mapping[str, object],
    repo_path_candidates: Callable[[str], Iterable[Path]],
) -> SymbolResolution | None:
    live_config = ledger.get("live_config") or {}
    if isinstance(live_config, Mapping):
        symbol = _deploy_symbol(stock_symbol_from_action_plan(live_config.get("action")))
        if symbol:
            return SymbolResolution(symbol, "run_ledger.live_config.action stock target")
        symbol = _deploy_symbol(live_config.get("symbol"))
        if symbol:
            return SymbolResolution(symbol, "run_ledger.live_config.symbol signal stream")
    spec_path = ledger.get("strategy_spec_path")
    if isinstance(spec_path, str) and spec_path:
        for candidate in repo_path_candidates(spec_path):
            try:
                spec = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            symbols = spec.get("symbols")
            if isinstance(symbols, list) and symbols:
                first = _deploy_symbol(symbols[0])
                if first:
                    return SymbolResolution(first, "strategy_spec.symbols fallback")
            break
    return None


def evaluate_deploy_start_admission(
    *,
    body: LiveInstanceDeployRequest,
    sid: str,
    visible_runs: Sequence[Mapping[str, object]],
    inherited_symbol: SymbolResolution | None,
    broker: InstanceBrokerView | None,
) -> DeployAdmissionBlock | None:
    if not body.start:
        return None
    return _identity_coherence_block(
        body,
        sid=sid,
        has_visible_runs=bool(visible_runs),
        inherited_symbol=inherited_symbol,
    ) or _exposure_coherence_block(
        body,
        sid=sid,
        visible_runs=visible_runs,
        broker=broker,
    )


def _identity_coherence_block(
    body: LiveInstanceDeployRequest,
    *,
    sid: str,
    has_visible_runs: bool,
    inherited_symbol: SymbolResolution | None,
) -> DeployAdmissionBlock | None:
    if inherited_symbol is None and (not sid or has_visible_runs):
        request_symbol = _deploy_symbol(body.inherited_symbol)
        inherited_symbol = (
            SymbolResolution(request_symbol, body.inherited_symbol_source or "request inherited symbol")
            if request_symbol
            else None
        )
    if inherited_symbol is None:
        return None

    live_config = body.live_config if isinstance(body.live_config, Mapping) else {}
    signal_stream = _deploy_symbol(live_config.get("symbol"))
    action_plan_symbol = _deploy_symbol(stock_symbol_from_action_plan(live_config.get("action")))
    facts = [
        {
            "label": "inherited_symbol",
            "value": inherited_symbol.value,
            "source": inherited_symbol.source,
        },
    ]
    if signal_stream:
        facts.append({"label": "signal_stream", "value": signal_stream, "source": "live_config.symbol"})
    if action_plan_symbol:
        facts.append({"label": "action_plan_symbol", "value": action_plan_symbol, "source": "live_config.action"})
    if len(facts) < 2 or len({fact["value"] for fact in facts}) == 1:
        return None
    confirmation = body.identity_coherence_confirmation
    if confirmation is not None and (
        _deploy_symbol(confirmation.inherited_symbol) == inherited_symbol.value
        and _deploy_symbol(confirmation.signal_stream) == signal_stream
        and _deploy_symbol(confirmation.action_plan_symbol) == action_plan_symbol
    ):
        return None

    compared = ", ".join(f"{fact['label']}={fact['value']}" for fact in facts)
    return DeployAdmissionBlock(
        status_code=409,
        detail={
            "reason_code": "IDENTITY_COHERENCE_UNCONFIRMED",
            "gate_id": "deploy.identity_coherence",
            "message": (
                "Deploy & start is blocked because the inherited bot symbol "
                f"does not match the new run identity ({compared}). Confirm "
                "the new run identity, or deploy without starting."
            ),
            "evidence": facts,
            "remediation": (
                "Review the inherited symbol, signal stream, and action plan. "
                "Then submit an identity_coherence_confirmation matching the "
                "current values, or turn off start."
            ),
        },
    )


def _exposure_coherence_block(
    body: LiveInstanceDeployRequest,
    *,
    sid: str,
    visible_runs: Sequence[Mapping[str, object]],
    broker: InstanceBrokerView | None,
) -> DeployAdmissionBlock | None:
    facts = _deploy_exposure_facts(body, sid=sid, visible_runs=visible_runs, broker=broker)
    if facts is None:
        return None
    if facts.posture == "FLAT" and facts.pending_order_count == 0:
        return None
    confirmation = body.exposure_coherence_confirmation
    if confirmation is not None and (
        confirmation.posture == facts.posture
        and confirmation.pending_order_count == facts.pending_order_count
        and confirmation.owned_positions == facts.owned_positions
        and confirmation.strategy_instance_id == facts.strategy_instance_id
        and confirmation.run_id == facts.run_id
    ):
        return None
    return DeployAdmissionBlock(
        status_code=409,
        detail={
            "reason_code": "EXPOSURE_COHERENCE_UNCONFIRMED",
            "gate_id": "deploy.exposure_coherence",
            "message": (
                "Deploy & start is blocked because existing exposure is not "
                f"proven flat (posture={facts.posture}, pending_order_count={facts.pending_order_count}). "
                "Confirm the exposure state, or deploy without starting."
            ),
            "evidence": facts.model_dump(mode="json"),
            "remediation": (
                "Review the bot's current risk and account reconciliation. "
                "Then submit an exposure_coherence_confirmation matching the "
                "current values, or turn off start."
            ),
        },
    )


def _deploy_exposure_facts(
    body: LiveInstanceDeployRequest,
    *,
    sid: str,
    visible_runs: Sequence[Mapping[str, object]],
    broker: InstanceBrokerView | None,
) -> ExposureCoherenceFacts | None:
    if sid and visible_runs:
        return compose_exposure_coherence_facts(
            broker,
            source="live_state.expected_position_by_symbol",
            strategy_instance_id=sid,
            run_id=str(visible_runs[0].get("run_id") or ""),
        )
    if body.inherited_exposure_posture is not None:
        return ExposureCoherenceFacts(
            posture=body.inherited_exposure_posture,
            pending_order_count=body.inherited_exposure_pending_order_count,
            owned_positions=normalize_exposure_positions(body.inherited_exposure_positions),
            source=body.inherited_exposure_source or "request inherited exposure",
            strategy_instance_id=sid or None,
            run_id=body.parent_run_id,
        )
    return None


def _deploy_symbol(value: object) -> str:
    return value.strip().upper() if isinstance(value, str) else ""
