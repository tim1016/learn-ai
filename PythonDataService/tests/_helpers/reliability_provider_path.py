"""The indicator reliability study's input path as it was before the lake (#3523), rebuilt from the code it still calls.

The router used to fetch Polygon aggregates in date chunks, keep the regular-hours bars with ``filter_session`` (called
without the bar length), run the indicator over warmup plus window, and trim the warmup at ET midnight of ``from_date``.
Each of those steps is still a function in the repo, so this module composes them the way the router did — the real
``fetch_bars_chunked`` and ``PolygonClientService.fetch_aggregates`` over the Polygon SDK's own ``Agg`` parser, fed a
pinned vendor payload — and tests compare its frame with the lake path's.

``filter_with_bar_length=False`` reproduces the old call exactly (``filter_session(df, "rth")``); ``True`` passes the
bars' own ``timespan`` and ``multiplier`` (the #3527 fix) so a test can say what the omission changed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from polygon.rest.models import Agg

from app.research import indicator_reliability as reliability_math
from app.services.dataset_service import (
    INDICATOR_CONFIGS,
    bar_minutes_for,
    calculate_dynamic_indicators,
    fetch_bars_chunked,
    filter_session,
    resolve_indicator_window,
)
from app.services.indicator_warmup_policy import requested_indicator_warmup_lookback
from app.services.polygon_client import PolygonClientService
from tests._helpers.pinned_vendor import VendorBar

_MINUTES_PER_UNIT = {"minute": 1, "hour": 60}


def vendor_fold(minutes: list[VendorBar], bar_minutes: int) -> list[VendorBar]:
    """An independent stand-in for the vendor's own N-minute aggregate: first/high/low/last/sum per wall-clock bucket."""
    bucket_ms = bar_minutes * 60_000
    out: dict[int, VendorBar] = {}
    for m in minutes:
        key = m["t"] // bucket_ms * bucket_ms
        if key not in out:
            out[key] = {**m, "t": key}
        else:
            bar = out[key]
            bar["h"], bar["l"], bar["c"], bar["v"] = max(bar["h"], m["h"]), min(bar["l"], m["l"]), m["c"], bar["v"] + m["v"]
    return [out[k] for k in sorted(out)]


class SdkTape:
    """The SDK's ``RESTClient.list_aggs`` over a pinned tape: it pages ``Agg`` objects exactly as the SDK would."""

    def __init__(self, raw: Mapping[date, list[VendorBar]], adjusted: Mapping[date, list[VendorBar]] | None) -> None:
        self.raw, self.adjusted = raw, adjusted

    def list_aggs(self, *, ticker, multiplier, timespan, from_, to, limit, adjusted, sort):
        tape = self.adjusted if (adjusted and self.adjusted is not None) else self.raw
        start, end = date.fromisoformat(from_), date.fromisoformat(to)
        minutes = [b for day, bars in sorted(tape.items()) if start <= day <= end for b in bars]
        bar_minutes = multiplier * _MINUTES_PER_UNIT[timespan]
        for bar in minutes if bar_minutes == 1 else vendor_fold(minutes, bar_minutes):
            yield Agg.from_dict(bar)


@dataclass
class ProviderFrame:
    """What the old study computed its statistics on, and where its warmup began."""

    frame: pd.DataFrame
    indicator_column: str
    window_start_ms: int
    fetch_from: str
    fetched_bars: int
    lead_in_bars: int


def provider_frame(
    raw: Mapping[date, list[VendorBar]],
    *,
    from_date: date,
    to_date: date,
    indicator_name: str = "rsi",
    indicator_params: Mapping[str, Any] | None = None,
    timespan: str = "minute",
    multiplier: int = 1,
    adjusted: Mapping[date, list[VendorBar]] | None = None,
    filter_with_bar_length: bool = False,
) -> ProviderFrame:
    """The old router's path from request to the frame the IC statistics ran on (the clock is the caller's to pin)."""
    entry = {"name": indicator_name, "params": dict(indicator_params or {"length": 14})}
    max_lookback = requested_indicator_warmup_lookback([entry], INDICATOR_CONFIGS)
    window = resolve_indicator_window(
        from_date.isoformat(), max_lookback=max_lookback, bar_minutes=bar_minutes_for(timespan, multiplier)
    )
    service = PolygonClientService()
    service.client = SdkTape(raw, adjusted)  # type: ignore[assignment]
    bars = fetch_bars_chunked(
        service,
        ticker="SPY",
        from_date=window.fetch_from,
        to_date=to_date.isoformat(),
        timespan=timespan,
        multiplier=multiplier,
        adjusted=True,
    )
    df = pd.DataFrame(bars)
    if filter_with_bar_length:
        df = filter_session(df, "rth", timespan=timespan, multiplier=multiplier)
    else:
        df = filter_session(df, "rth")  # exactly the call the router made: no timespan, no multiplier (#3527)
    df, meta = calculate_dynamic_indicators(df, [entry])
    column = meta[0]["column"]
    lead_in = int((df["timestamp"] < window.window_start_ms).sum())
    df = df[df["timestamp"] >= window.window_start_ms].reset_index(drop=True)
    return ProviderFrame(df, column, window.window_start_ms, window.fetch_from, len(bars), lead_in)


def seeded_baseline(monkeypatch, seed: int = 7) -> None:
    """Make the random-shuffle baseline reproducible: each call draws from a fresh generator with the same seed."""
    original = reliability_math.compute_random_baseline_ic

    def seeded(*args: Any, **kwargs: Any):
        return original(*args, **{**kwargs, "rng": np.random.default_rng(seed)})

    monkeypatch.setattr(reliability_math, "compute_random_baseline_ic", seeded)
