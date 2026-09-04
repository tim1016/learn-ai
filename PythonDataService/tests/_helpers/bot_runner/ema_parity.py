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
from decimal import Decimal
from pathlib import Path

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.marketdata.feed import MarketDataBar

_EMA_FIRST_EXIT_MS = 1_770_393_600_000


def _ema_parity_bars_through_first_exit() -> list[MarketDataBar]:
    """Load the retained LEAN input stream through its first EMA round-trip."""
    fixture = (
        Path(__file__).resolve().parents[2]
        / "fixtures/golden/cross-engine-studies/cells"
        / "SPY_W3mo_2026-02-02_to_2026-04-30/lean/observations.csv"
    )
    bars: list[MarketDataBar] = []
    with fixture.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            end_ms = int(row["ms_utc"])
            bars.append(
                MarketDataBar(
                    symbol="SPY",
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
            if end_ms > _EMA_FIRST_EXIT_MS:
                break
    return bars


def _ema_signal_evaluation_id(bar_close_ms: int, *, symbol: str = "SPY") -> str:
    """Independently recompute the Signal Program's documented evaluation
    identity (see the Formula note in ``app/engine/strategy/signal_program.py``:
    SHA-256 of the canonical JSON of program version, settings, and bar-close
    clock) from the real registered strategy -- not a hand-typed guess at the
    hash bytes. Proves ``decision_id`` really is the deterministic per-bar
    Signal Program identity the PRD requires (``decision_id = evaluation_id``,
    issue #1728 / PRD section 16), rather than merely echoing whatever the
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
