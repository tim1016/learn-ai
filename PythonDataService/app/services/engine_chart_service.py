"""Compose exact engine bars with canonical strategy indicator evidence.

The policy store is read once. Strategy-owned series replay those bars through
the same streaming indicator classes used by the engine; optional exploratory
series continue to use the generic chart service on that caller-owned frame.
No numerical formula is implemented here.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from app.engine.data.policy_store import policy_key, resolve_data_roots
from app.engine.data.trade_bar import TradeBar
from app.engine.indicators.macd import MovingAverageConvergenceDivergence
from app.engine.indicators.supertrend import Supertrend
from app.engine.strategy.registry import _STRATEGY_REGISTRY, ChartParamRef
from app.engine.strategy.spec.indicators import build_indicator, is_bar_indicator
from app.engine.strategy.spec.schema import IndicatorBlock
from app.lean_sidecar.trading_calendar import SessionWindow, session_windows_ms_utc
from app.schemas.chart import ChartIndicatorResult
from app.schemas.engine_chart import (
    EngineChartBar,
    EngineChartCoverage,
    EngineChartRequest,
    EngineChartResponse,
    ResolvedChartIndicator,
)
from app.services.chart_service import compute_indicator_results, indicator_presentation
from app.services.engine_bars_service import read_consolidated_bars
from app.utils.timestamps import to_ms_utc

_ET = ZoneInfo("America/New_York")


def build_engine_chart(request: EngineChartRequest) -> EngineChartResponse:
    """Read one exact bar set and compute every requested series on it."""
    from app.lean_sidecar.workspace import validate_symbol

    registration = _STRATEGY_REGISTRY.get(request.strategy_name)
    if registration is None:
        raise ValueError(f"unknown strategy: {request.strategy_name}")
    hidden = registration.hidden_params.intersection(request.parameters)
    if hidden:
        raise ValueError(f"hidden strategy parameters are not accepted: {', '.join(sorted(hidden))}")

    validated = registration.param_schema.model_validate(request.parameters)
    safe_symbol = validate_symbol(request.symbol)
    start, end, sessions = _resolve_window(request.from_ms_utc, request.to_ms_utc)

    consolidated = read_consolidated_bars(
        roots=resolve_data_roots(source="polygon", adjusted=request.adjusted),
        symbol=safe_symbol,
        start=start,
        end=end,
        session=request.session,
        timespan=request.timespan,
        multiplier=request.multiplier,
    )
    bars = [
        EngineChartBar(
            t=to_ms_utc(bar.end_time),
            o=float(bar.open),
            h=float(bar.high),
            l=float(bar.low),
            c=float(bar.close),
            v=int(bar.volume),
        )
        for bar in consolidated.bars
    ]

    excluded = set(request.excluded_strategy_indicator_ids)
    default_specs: list[tuple[str, dict[str, int | float]]] = []
    for template in registration.chart_indicators:
        params = {name: _resolve_param(value, validated) for name, value in template.params.items()}
        if indicator_id(template.name, params) not in excluded:
            default_specs.append((template.name, params))

    seen = {_indicator_key(name, params) for name, params in default_specs}
    requested_specs: list[tuple[str, dict[str, int | float]]] = []
    for entry in request.indicators:
        key = _indicator_key(entry.name, entry.params)
        if key in seen:
            continue
        seen.add(key)
        requested_specs.append((entry.name, entry.params))

    dataframe = pd.DataFrame(
        {
            "timestamp": [bar.t for bar in bars],
            "open": [bar.o for bar in bars],
            "high": [bar.h for bar in bars],
            "low": [bar.l for bar in bars],
            "close": [bar.c for bar in bars],
            "volume": [bar.v for bar in bars],
        }
    )
    strategy_results = compute_strategy_indicator_results(consolidated.bars, default_specs)
    requested_dicts = [{"name": name, "params": params} for name, params in requested_specs]
    exploratory_results = (
        compute_indicator_results(dataframe, requested_dicts) if requested_dicts and not dataframe.empty else []
    )

    resolved_specs = [
        ResolvedChartIndicator(
            id=indicator_id(name, params),
            name=name,
            params=params,
            label=indicator_label(name, params),
            strategy_default=strategy_default,
        )
        for name, params, strategy_default in [
            *((name, params, True) for name, params in default_specs),
            *((name, params, False) for name, params in requested_specs),
        ]
    ]
    missing_dates = set(consolidated.coverage.missing_days)
    missing_session_ms = [window.open_ms_utc for window in sessions if window.session_date in missing_dates]

    return EngineChartResponse(
        policy_key=policy_key(source="polygon", adjusted=request.adjusted),
        symbol=safe_symbol,
        bars=bars,
        coverage=EngineChartCoverage(
            expected_days=len(sessions),
            available_days=len(sessions) - len(missing_session_ms),
            is_complete=not missing_session_ms,
            missing_session_ms_utc=missing_session_ms,
        ),
        indicator_specs=resolved_specs,
        indicators=[*strategy_results, *exploratory_results],
    )


def compute_strategy_indicator_results(
    bars: list[TradeBar],
    specs: list[tuple[str, dict[str, int | float]]],
) -> list[ChartIndicatorResult]:
    """Replay bars through canonical engine indicators with engine timestamps."""
    results: list[ChartIndicatorResult] = []
    for name, params in specs:
        indicator = build_indicator(_indicator_block(name, params))
        points: list[dict[str, int | float | None]] = []
        macd_points = {"macd": [], "signal": [], "histogram": []}
        trend_points = {"up": [], "down": []}

        for bar in bars:
            if is_bar_indicator(indicator):
                indicator.update(bar)
            else:
                indicator.update(bar.end_time, bar.close)
            timestamp = to_ms_utc(bar.end_time)
            if isinstance(indicator, MovingAverageConvergenceDivergence):
                macd_points["macd"].append(_point(timestamp, indicator.macd))
                macd_points["signal"].append(_point(timestamp, indicator.signal))
                macd_points["histogram"].append(_point(timestamp, indicator.histogram))
            elif isinstance(indicator, Supertrend):
                value = indicator.current_value
                trend_points["up"].append(_point(timestamp, value if indicator.is_long else None))
                trend_points["down"].append(_point(timestamp, value if indicator.is_long is False else None))
            else:
                points.append(_point(timestamp, indicator.current_value))

        panel, color, refs = indicator_presentation(name)
        data: Any
        result_type = "line"
        if isinstance(indicator, MovingAverageConvergenceDivergence):
            data = macd_points
            result_type = "macd"
        elif isinstance(indicator, Supertrend):
            data = trend_points
        else:
            data = points
        results.append(
            ChartIndicatorResult(
                id=indicator_id(name, params),
                panel=panel,
                type=result_type,
                color=color,
                data=data,
                refs=refs,
            )
        )
    return results


def indicator_id(name: str, params: dict[str, int | float]) -> str:
    signature = "-".join(f"{key}-{_number_token(params[key])}" for key in sorted(params))
    return f"{name}-{signature}" if signature else name


def indicator_label(name: str, params: dict[str, int | float]) -> str:
    values = "/".join(_number_token(value) for value in params.values())
    return f"{name.upper()} {values}" if values else name.upper()


def _indicator_key(name: str, params: dict[str, int | float]) -> tuple[str, tuple[tuple[str, str], ...]]:
    return name, tuple((key, _number_token(value)) for key, value in sorted(params.items()))


def _number_token(value: int | float) -> str:
    if isinstance(value, bool):
        raise ValueError("indicator parameters must be numeric, not boolean")
    number = float(value)
    return str(int(number)) if number.is_integer() else format(number, ".15g")


def _resolve_param(value: int | float | ChartParamRef, validated: object) -> int | float:
    resolved = getattr(validated, value.field) if isinstance(value, ChartParamRef) else value
    if isinstance(resolved, bool) or not isinstance(resolved, (int, float)):
        raise ValueError("chart recipe parameters must resolve to numbers")
    return resolved


def _indicator_block(name: str, params: dict[str, int | float]) -> IndicatorBlock:
    normalized = name.lower()
    if normalized in {"sma", "ema", "rsi", "adx"}:
        return IndicatorBlock(id=indicator_id(name, params), kind=normalized.upper(), period=_period(params, "length"))
    if normalized == "macd":
        return IndicatorBlock(
            id=indicator_id(name, params),
            kind="MACD",
            period=_period(params, "slow"),
            fast_period=_period(params, "fast"),
            signal_period=_period(params, "signal"),
        )
    if normalized == "supertrend":
        return IndicatorBlock(
            id=indicator_id(name, params),
            kind="SUPERTREND",
            period=_period(params, "length"),
            multiplier=float(params["multiplier"]),
        )
    raise ValueError(f"strategy chart recipe uses unsupported canonical indicator: {name}")


def _period(params: dict[str, int | float], name: str) -> int:
    value = params.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise ValueError(f"indicator parameter {name!r} must be an integer")
    return int(value)


def _point(timestamp: int, value: object) -> dict[str, int | float | None]:
    return {"t": timestamp, "value": None if value is None else float(value)}


def _resolve_window(from_ms_utc: int, to_ms_utc: int) -> tuple[date, date, list[SessionWindow]]:
    from_date = datetime.fromtimestamp(from_ms_utc / 1000, tz=UTC).astimezone(_ET).date()
    to_date = datetime.fromtimestamp(to_ms_utc / 1000, tz=UTC).astimezone(_ET).date()
    windows = session_windows_ms_utc(from_date, to_date)
    start_index = next((index for index, window in enumerate(windows) if window.open_ms_utc == from_ms_utc), None)
    end_index = next((index for index, window in enumerate(windows) if window.open_ms_utc == to_ms_utc), None)
    if start_index is None or end_index is None or end_index <= start_index:
        raise ValueError("chart window must use ordered NYSE session-open millisecond anchors")
    selected = windows[start_index:end_index]
    return selected[0].session_date, selected[-1].session_date, selected
