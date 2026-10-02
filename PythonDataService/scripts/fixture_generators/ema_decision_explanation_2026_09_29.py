"""Generate EXP-001: the 2026-09-29 EMA decision explanations (#2639).

Input is the live bot ``spy-ema-20260929-1432``'s own retained IBKR minutes
(its ``source_bars.sqlite3``), regular-session bars only, exactly as the live
runner consolidated them. Output is the canonical EMA Signal Program's
decision explanation on the 14:30 and 14:45 ET decision bars, replayed from
the first retained session (2026-09-23).

Usage (from PythonDataService/):
    .venv/bin/python -m scripts.fixture_generators.ema_decision_explanation_2026_09_29 \\
        --ledger /path/to/source_bars.sqlite3

``--ledger`` is the bot's ledger copied out of the live Clerk's volume
(``accounts/alpaca/live-evidence:spy-ema-20260929-1432/source_bars.sqlite3``).
Without it the committed ``input.json`` is replayed again, which is how the
output is regenerated after a deliberate strategy change.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

_FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
sys.path.insert(0, str(_FIXTURES))

from golden_support.strategy_replay import staged_decisions  # noqa: E402

from app.engine.data.trade_bar import TradeBar  # noqa: E402
from app.engine.strategy.registry import _STRATEGY_REGISTRY  # noqa: E402
from app.engine.strategy.signal_program import SignalDecision  # noqa: E402
from app.schemas.decision_explanation import DecisionExplanationRecord  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parents[2] / ("tests/fixtures/golden/strategy-explanation/EXP-001/v1")
SYMBOL = "SPY"
# The two decision bars the 2026-09-29 investigation turned on, by close:
# 14:30 and 14:45 ET (18:30 and 18:45 UTC, EDT).
DECISION_CLOSES_MS = (1_790_706_600_000, 1_790_707_500_000)


def read_ledger_rth_minutes(ledger: Path) -> list[dict[str, Any]]:
    """The bot's retained regular-session IBKR minutes, in delivery order."""
    conn = sqlite3.connect(f"file:{ledger}?mode=ro&immutable=1", uri=True)
    try:
        rows = conn.execute(
            "SELECT start_ms, end_ms, open, high, low, close, volume FROM source_bars "
            "WHERE provider = 'ibkr' AND symbol = ? AND session_phase = 'RTH' ORDER BY end_ms",
            (SYMBOL,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"start_ms": s, "end_ms": e, "open": o, "high": h, "low": low, "close": c, "volume": v}
        for s, e, o, h, low, c, v in rows
    ]


def minute_bars(minutes: list[dict[str, Any]]) -> list[TradeBar]:
    """The fixture's minutes as engine bars, prices exactly as retained."""
    return [
        TradeBar(
            symbol=SYMBOL,
            start_ms=row["start_ms"],
            end_ms=row["end_ms"],
            open=Decimal(row["open"]),
            high=Decimal(row["high"]),
            low=Decimal(row["low"]),
            close=Decimal(row["close"]),
            volume=int(row["volume"]),
        )
        for row in minutes
    ]


def replay(
    minutes: list[dict[str, Any]],
    *,
    program_key: str = "ema_crossover_signal",
    settings: dict[str, Any] | None = None,
) -> list[tuple[TradeBar, SignalDecision]]:
    """Every (decision bar, decision) a registered program stages over ``minutes``."""
    registration = _STRATEGY_REGISTRY[program_key]
    strategy = registration.build(registration.param_schema(**{**(settings or {}), "symbol": SYMBOL}))
    return staged_decisions(strategy, minute_bars(minutes))


def expected_explanations(minutes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_close = {bar.end_ms: (bar, decision) for bar, decision in replay(minutes)}
    output = []
    for close_ms in DECISION_CLOSES_MS:
        bar, decision = by_close[close_ms]
        record = DecisionExplanationRecord.from_decision(bar, decision)
        assert record is not None
        output.append(record.model_dump(mode="json"))
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, default=None)
    args = parser.parse_args()
    input_path = FIXTURE_DIR / "input.json"
    if args.ledger is not None:
        minutes = read_ledger_rth_minutes(args.ledger)
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        input_path.write_text(json.dumps({"symbol": SYMBOL, "minutes": minutes}, indent=1) + "\n", encoding="utf-8")
    minutes = json.loads(input_path.read_text(encoding="utf-8"))["minutes"]
    output = {"decision_closes_ms": list(DECISION_CLOSES_MS), "explanations": expected_explanations(minutes)}
    (FIXTURE_DIR / "output.json").write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
