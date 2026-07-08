"""Server-authored bot catalog projection.

The catalog is an operator-facing summary of ``LiveInstanceStatus``.  Keep the
meaning composition here so Angular filters/renders closed fields instead of
inferring operational state from raw cockpit evidence.
"""

from __future__ import annotations

from typing import Literal

from app.schemas.live_runs import (
    BotCatalogMetrics,
    BotCatalogPnl,
    BotCatalogRow,
    LiveInstanceStatus,
)

TradingMode = Literal["paper", "live", "unknown"]
CatalogTone = Literal["positive", "warning", "danger", "neutral"]


def compose_bot_catalog_row(status: LiveInstanceStatus, trading_mode: TradingMode) -> BotCatalogRow:
    error_count = _error_count(status)
    readiness_verdict = status.readiness.verdict if status.readiness is not None else "UNKNOWN"
    prior_run_classification = status.operator_surface.prior_run.classification
    return BotCatalogRow(
        strategy_instance_id=status.strategy_instance_id,
        name=status.strategy_instance_id,
        status_label=_status_label(status),
        status_detail=_status_detail(status),
        status_tone=_status_tone(readiness_verdict, error_count),
        only_fresh_run_available=status.lifecycle_chart.only_fresh_run_available,
        needs_attention=error_count > 0 or readiness_verdict in ("BLOCKED", "DEGRADED"),
        trading_mode=trading_mode,
        symbols=_symbols(status),
        engine=status.start_defaults.strategy if status.start_defaults is not None else None,
        engine_asset_class=_engine_asset_class(status),
        created_at_ms=status.provenance.created_at_ms if status.provenance is not None else None,
        updated_at_ms=status.desired_state.updated_at_ms if status.desired_state is not None else None,
        last_run_at_ms=_last_run_at_ms(status),
        last_run_label=_last_run_label(prior_run_classification),
        last_run_result=prior_run_classification,
        last_run_detail=_last_run_detail(status),
        process_state=status.operator_surface.host_process.state,
        desired_state=status.desired_state.state if status.desired_state is not None else None,
        readiness_verdict=readiness_verdict,
        metrics=BotCatalogMetrics(
            pnl=BotCatalogPnl(
                unrealized=status.operator_surface.current_risk.unrealized_pnl,
            ),
            current_exposure=_exposure(status),
            open_positions=_open_position_count(status),
            error_count=error_count,
        ),
    )


def trading_mode_from_configured_mode(value: object) -> TradingMode:
    return value if value in ("paper", "live") else "unknown"


def _symbols(status: LiveInstanceStatus) -> list[str]:
    symbols: set[str] = set()
    if status.symbol:
        symbols.add(status.symbol)
    if status.broker is not None:
        symbols.update(symbol for symbol in status.broker.owned_positions if symbol)
    return sorted(symbols)


def _exposure(status: LiveInstanceStatus) -> str:
    positions = status.broker.owned_positions if status.broker is not None else {}
    active = [(symbol, qty) for symbol, qty in positions.items() if qty != 0]
    if not active:
        return "Flat"
    return ", ".join(f"{symbol} {qty:g}" for symbol, qty in sorted(active))


def _open_position_count(status: LiveInstanceStatus) -> int | None:
    if status.broker is None:
        return None
    return sum(1 for qty in status.broker.owned_positions.values() if qty != 0)


def _error_count(status: LiveInstanceStatus) -> int:
    operator = status.operator_surface
    count = 0
    if operator.incident_headline is not None:
        count += 1
    if operator.runtime_freshness is not None:
        if operator.runtime_freshness.headline is not None:
            count += 1
        count += len(operator.runtime_freshness.additional_reasons)
    count += sum(1 for gate in operator.readiness_gates if gate.status != "pass")
    return count


def _status_tone(readiness_verdict: str, error_count: int) -> CatalogTone:
    if error_count > 0 or readiness_verdict == "BLOCKED":
        return "danger"
    if readiness_verdict == "DEGRADED":
        return "warning"
    if readiness_verdict == "READY":
        return "positive"
    return "neutral"


def _status_label(status: LiveInstanceStatus) -> str:
    if status.readiness is not None:
        match status.readiness.verdict:
            case "READY":
                return "Ready"
            case "DEGRADED":
                return "Degraded"
            case "BLOCKED":
                return "Blocked"
            case _:
                return "Unknown"
    notice = status.operator_surface.host_process.notice
    if notice:
        return notice
    return status.operator_surface.host_process.state


def _status_detail(status: LiveInstanceStatus) -> str | None:
    if status.readiness is not None and status.readiness.summary:
        return status.readiness.summary
    return status.operator_surface.host_process.notice


def _last_run_label(classification: str) -> str:
    match classification:
        case "CLEAN":
            return "Clean"
        case "HALT_TRIGGERED":
            return "Safety halt"
        case "EXITED_WITH_ERROR":
            return "Exited with error"
        case _:
            return "No result yet"


def _last_run_detail(status: LiveInstanceStatus) -> str | None:
    last_exit = status.last_exit
    classification = status.operator_surface.prior_run.classification
    if last_exit is None:
        return "No completed run has been recorded for this bot."
    details: list[str] = []
    if classification == "CLEAN":
        details.append("Previous run exited normally.")
    elif classification == "HALT_TRIGGERED":
        trigger = _human_code(last_exit.halt_trigger) if last_exit.halt_trigger else "Safety halt"
        details.append(f"Previous run stopped on a safety halt: {trigger}.")
    elif classification == "EXITED_WITH_ERROR":
        reason = _exit_reason_label(str(last_exit.exit_reason)) if last_exit.exit_reason is not None else None
        if reason is not None:
            details.append(f"Previous run exited with an error: {reason}.")
        else:
            details.append("Previous run exited with an error.")
    else:
        details.append("The previous run result is unknown.")
    if last_exit.exit_code is not None:
        details.append(f"Exit code {last_exit.exit_code}.")
    if last_exit.hydration_accepted is False:
        failure = _human_code(last_exit.hydration_failure_reason) if last_exit.hydration_failure_reason else "not accepted"
        details.append(f"Indicator-state hydration was {failure}.")
    return " ".join(details)


def _exit_reason_label(reason: str) -> str:
    labels = {
        "normal": "normal shutdown",
        "force_flat_complete": "force-flat completed",
        "keyboard_interrupt": "keyboard interrupt",
        "signal": "OS signal",
        "max_orders_exceeded": "daily order cap reached",
        "fatal_halt": "fatal safety halt",
        "recovery_flatten": "recovery flatten",
        "exception": "runtime exception",
        "poisoned": "poisoned run refused",
    }
    return labels.get(reason, _human_code(reason))


def _human_code(value: str | None) -> str:
    if not value:
        return "unknown"
    return value.replace("_", " ").replace("-", " ").strip().capitalize()


def _engine_asset_class(status: LiveInstanceStatus) -> str | None:
    live_config = status.provenance.live_config if status.provenance is not None else {}
    for key in ("asset_class", "engine_asset_class"):
        value = live_config.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return status.instrument_surface


def _last_run_at_ms(status: LiveInstanceStatus) -> int | None:
    if status.last_exit is not None and status.last_exit.ended_at_ms is not None:
        return status.last_exit.ended_at_ms
    if status.process.started_at_ms is not None:
        return status.process.started_at_ms
    return status.readiness.as_of_ms if status.readiness is not None else None
