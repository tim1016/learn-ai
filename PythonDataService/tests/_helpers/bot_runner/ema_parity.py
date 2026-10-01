"""LEAN-parity EMA-crossover bar fixture and the Signal Program's
deterministic per-bar evaluation identity, shared by the bot_runner test
package and downstream replay/crash suites.

Split out of ``tests/services/bot_runner/conftest.py`` per issue #1810 --
see that module's sibling ``doubles.py``/``custody.py``/``market.py`` for
the other extracted themes.
"""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.marketdata.feed import MarketDataBar

_EMA_FIRST_EXIT_MS = 1_770_393_600_000
_LEAN_CELLS = Path(__file__).resolve().parents[2] / "fixtures/golden/cross-engine-studies/cells"


def lean_cell_bars(cell: str, *, symbol: str, stop_after_ms: int) -> list[MarketDataBar]:
    """The retained LEAN input stream of one cross-engine cell, through the first bar after ``stop_after_ms``."""
    bars: list[MarketDataBar] = []
    with (_LEAN_CELLS / cell / "lean/observations.csv").open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            end_ms = int(row["ms_utc"])
            bars.append(
                MarketDataBar(
                    symbol=symbol,
                    start_ms=end_ms - 60_000,
                    end_ms=end_ms,
                    open=Decimal(row["open"]),
                    high=Decimal(row["high"]),
                    low=Decimal(row["low"]),
                    close=Decimal(row["close"]),
                    volume=int(Decimal(row["volume"])),
                    fetched_at_ms=end_ms + 100,
                    feed_id="lean-golden",
                    session_phase="RTH",
                )
            )
            if end_ms > stop_after_ms:
                break
    return bars


def _ema_parity_bars_through_first_exit() -> list[MarketDataBar]:
    """Load the retained LEAN input stream through its first EMA round-trip."""
    return lean_cell_bars(
        "SPY_W3mo_2026-02-02_to_2026-04-30", symbol="SPY", stop_after_ms=_EMA_FIRST_EXIT_MS
    )


EMA_LAST_BAR_ENTER_DAY = date(2026, 2, 3)
"""The day QQQ's first EMA ENTER is decided on the session's last bucket (15:45-16:00).

LEAN's own run submitted that market order at the 16:00 close and filled it at
the next open -- an overnight entry nobody decided (#2596)."""


def ema_bars_through_a_last_bar_enter() -> list[MarketDataBar]:
    """QQQ's retained LEAN input stream through its first EMA ENTER, decided at the close."""
    return lean_cell_bars(
        "QQQ_W3mo_2026-02-02_to_2026-04-30",
        symbol="QQQ",
        stop_after_ms=session_close_ms_utc(EMA_LAST_BAR_ENTER_DAY) - 1,
    )


def _ema_signal_evaluation_id(bar_close_ms: int, *, symbol: str = "SPY") -> str:
    """Independently recompute the Signal Program's documented evaluation
    identity (see the Formula note in ``app/engine/strategy/signal_program.py``:
    SHA-256 of the canonical JSON of program version, settings, and bar-close
    clock) from the real registered strategy -- not a hand-typed guess at the
    hash bytes. Proves ``decision_id`` really is the deterministic per-bar
    Signal Program identity ADR 0043 decision 5 requires
    (``decision_id = evaluation_id``, issue #1728), rather than merely echoing whatever the
    current build happens to emit.
    """
    registration = _STRATEGY_REGISTRY["ema_crossover_signal"]
    assert registration.signal_program_factory is not None
    params = registration.param_schema(symbol=symbol)
    program = registration.signal_program_factory(params)
    payload = {
        "program_key": program.session.program_key,
        "program_version": program.session.program_version,
        "settings": program.strategy.signal_program_settings(),
        "bar_close_ms": bar_close_ms,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
