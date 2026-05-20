"""Shared parity-assertion helpers for LEAN-vs-engine receipts."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

_FLOAT_FIELDS = ("close", "ema_fast", "ema_slow", "rsi")
_EXACT_FIELDS = ("ts_ms_utc", "cross_state", "signal")


def assert_state_traces_match(
    lean_rows: list[dict[str, Any]],
    engine_rows: list[dict[str, Any]],
    *,
    atol: float,
    rtol: float,
) -> None:
    """Assert LEAN's state.csv == engine's recorded snapshots row-by-row.

    First divergence raises AssertionError with both sides' full row
    and the field that broke tolerance.
    """
    if len(lean_rows) != len(engine_rows):
        raise AssertionError(f"state-trace row count mismatch: lean={len(lean_rows)}, engine={len(engine_rows)}")

    for i, (lr, er) in enumerate(zip(lean_rows, engine_rows, strict=True)):
        for field in _EXACT_FIELDS:
            if lr[field] != er[field]:
                raise AssertionError(
                    f"row {i}: exact-field {field!r} differs: "
                    f"lean={lr[field]!r} engine={er[field]!r}\n"
                    f"  lean row : {lr}\n  engine row: {er}"
                )
        for field in _FLOAT_FIELDS:
            lv, ev = float(lr[field]), float(er[field])
            if not math.isclose(lv, ev, abs_tol=atol, rel_tol=rtol):
                raise AssertionError(
                    f"row {i}: float-field {field!r} differs beyond "
                    f"atol={atol}, rtol={rtol}: lean={lv!r} engine={ev!r}\n"
                    f"  lean row : {lr}\n  engine row: {er}"
                )


def assert_trade_equivalence(
    lean_trades: list[dict[str, Any]],
    engine_trades: list[dict[str, Any]],
    *,
    fill_price_atol: Decimal,
) -> None:
    """Assert LEAN's trade list == engine's trade list within fill-price tolerance.

    Exact match on timestamps and quantities; entry/exit prices within
    ``fill_price_atol`` (default $0.01 per the divergence taxonomy's
    FILL_PRICE_DRIFT category).
    """
    if len(lean_trades) != len(engine_trades):
        raise AssertionError(f"trade count mismatch: lean={len(lean_trades)}, engine={len(engine_trades)}")

    for i, (lt, et) in enumerate(zip(lean_trades, engine_trades, strict=True)):
        for field in ("entry_ms_utc", "exit_ms_utc", "quantity"):
            if lt[field] != et[field]:
                raise AssertionError(
                    f"trade {i}: {field!r} differs: lean={lt[field]!r} engine={et[field]!r}\n"
                    f"  lean : {lt}\n  engine: {et}"
                )
        for field in ("entry_price", "exit_price"):
            diff = abs(Decimal(str(lt[field])) - Decimal(str(et[field])))
            if diff > fill_price_atol:
                raise AssertionError(
                    f"trade {i}: {field!r} differs beyond {fill_price_atol}: "
                    f"lean={lt[field]} engine={et[field]} diff={diff}\n"
                    f"  lean : {lt}\n  engine: {et}"
                )
