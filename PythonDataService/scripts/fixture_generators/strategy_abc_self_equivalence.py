"""Generate the ENG-008 Strategy A/B/C self-equivalence golden fixture (#1699).

The reference is the *current* (pre-port) Strategy A/B/C math itself, run
once and pinned here — not an external oracle. This is a
``reference_kind=internal_regression`` receipt: it does not certify that
Strategy A/B/C are correct, only that a future refactor (the S3 intent
port) reproduces the exact same ``trade_log`` the strategies produce
today. See ``docs/references/strategy-abc-self-equivalence.md``.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from golden_support.hashing import compute_hashes  # noqa: E402
from golden_support.strategy_replay import run_strategy_over_bars  # noqa: E402

from app.engine.data.trade_bar import TradeBar  # noqa: E402
from app.engine.strategy.algorithms.spy_strategy_a import SpyStrategyAAlgorithm  # noqa: E402
from app.engine.strategy.algorithms.spy_strategy_b import SpyStrategyBAlgorithm  # noqa: E402
from app.engine.strategy.algorithms.spy_strategy_c import SpyStrategyCAlgorithm  # noqa: E402
from app.engine.strategy.base import LoggedTrade  # noqa: E402
from app.utils.timestamps import to_ms_utc  # noqa: E402

FIXTURE_ID = "ENG-008"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "golden" / "strategy-parity" / FIXTURE_ID / "v1"
MANIFEST_PATH = ROOT / "tests" / "fixtures" / "golden" / "manifest.json"

SEED = 20260820
BAR_COUNT = 900
RESOLUTION_MINUTES = 15
SYMBOL = "SPY"
BASE_PRICE = 400.0
STEP_STD = 1.35
NY = ZoneInfo("America/New_York")
START_TIME = datetime(2024, 1, 2, 9, 30, tzinfo=NY)


def build_bars() -> list[TradeBar]:
    """A seeded random-walk 15-min SPY-like bar series (one bar per real 15-min step).

    Follows the same "one input bar exactly spans one consolidator window"
    trick used by the repo's existing strategy-parity synthetic fixtures
    (``app/engine/strategy/spec/tests/_parity_helpers.py::build_minute_bars``):
    RsiRangeStrategy's 15-minute consolidator emits the fed bar unchanged
    when bars already arrive one per 15-minute boundary.
    """
    rng = np.random.default_rng(SEED)
    closes = [BASE_PRICE]
    for _ in range(BAR_COUNT - 1):
        closes.append(closes[-1] + float(rng.normal(0.0, STEP_STD)))
    closes_arr = np.array(closes)
    highs = closes_arr + rng.uniform(0.1, 1.0, BAR_COUNT)
    lows = closes_arr - rng.uniform(0.1, 1.0, BAR_COUNT)
    opens = closes_arr + rng.uniform(-0.5, 0.5, BAR_COUNT)
    highs = np.maximum(highs, np.maximum(opens, closes_arr))
    lows = np.minimum(lows, np.minimum(opens, closes_arr))
    volumes = rng.integers(500, 2000, BAR_COUNT)

    bars: list[TradeBar] = []
    for i in range(BAR_COUNT):
        t = START_TIME + timedelta(minutes=RESOLUTION_MINUTES * i)
        bars.append(
            TradeBar(
                symbol=SYMBOL,
                start_ms=to_ms_utc(t),
                end_ms=to_ms_utc(t + timedelta(minutes=1)),
                open=Decimal(str(round(float(opens[i]), 4))),
                high=Decimal(str(round(float(highs[i]), 4))),
                low=Decimal(str(round(float(lows[i]), 4))),
                close=Decimal(str(round(float(closes_arr[i]), 4))),
                volume=int(volumes[i]),
            )
        )
    return bars


def _trade_to_json(trade: LoggedTrade) -> dict:
    return {
        "entry_time_ms": trade.entry_time_ms,
        "entry_price": str(trade.entry_price),
        "exit_time_ms": trade.exit_time_ms,
        "exit_price": str(trade.exit_price),
        "quantity": trade.quantity,
        "pnl_pts": str(trade.pnl_pts),
        "pnl_pct": str(trade.pnl_pct),
        "result": trade.result,
        "indicators": {name: str(value) for name, value in sorted(trade.indicators.items())},
        "signal_reason": trade.signal_reason,
        "is_synthetic_exit": trade.is_synthetic_exit,
    }


def _write_bars_arrow(path: Path, bars: list[TradeBar]) -> None:
    price_type = pa.decimal128(18, 6)
    table = pa.table(
        {
            "symbol": pa.array([bar.symbol for bar in bars], type=pa.string()),
            "time_ms_utc": pa.array([bar.start_ms for bar in bars], type=pa.int64()),
            "end_time_ms_utc": pa.array([bar.end_ms for bar in bars], type=pa.int64()),
            "open": pa.array([bar.open for bar in bars], type=price_type),
            "high": pa.array([bar.high for bar in bars], type=price_type),
            "low": pa.array([bar.low for bar in bars], type=price_type),
            "close": pa.array([bar.close for bar in bars], type=price_type),
            "volume": pa.array([bar.volume for bar in bars], type=pa.int64()),
        }
    )
    with pa.ipc.new_file(path, table.schema) as writer:
        writer.write_table(table)


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    bars = build_bars()

    trades_by_strategy = {
        "strategy_a": run_strategy_over_bars(SpyStrategyAAlgorithm(), bars),
        "strategy_b": run_strategy_over_bars(SpyStrategyBAlgorithm(), bars),
        "strategy_c": run_strategy_over_bars(SpyStrategyCAlgorithm(), bars),
    }
    for key, trades in trades_by_strategy.items():
        if not trades:
            raise RuntimeError(f"{key} produced zero trades against the synthetic series — retune STEP_STD/SEED")

    input_path = FIXTURE_DIR / "input.arrow"
    output_path = FIXTURE_DIR / "output.json"
    attribution_path = FIXTURE_DIR / "attribution.md"

    _write_bars_arrow(input_path, bars)
    _write_json(
        output_path,
        {
            "seed": SEED,
            "bar_count": BAR_COUNT,
            "resolution_minutes": RESOLUTION_MINUTES,
            "trade_counts": {key: len(trades) for key, trades in trades_by_strategy.items()},
            "trades": {key: [_trade_to_json(trade) for trade in trades] for key, trades in trades_by_strategy.items()},
        },
    )
    _write_attribution(attribution_path, trades_by_strategy)

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest["fixtures"] = [item for item in manifest["fixtures"] if item["id"] != FIXTURE_ID]
    content_hashes, file_hashes = compute_hashes(FIXTURE_DIR, ["input.arrow", "output.json"])
    file_hashes["attribution.md"] = hashlib.sha256(attribution_path.read_bytes()).hexdigest()
    manifest["fixtures"].append(
        {
            "id": FIXTURE_ID,
            "name": "Strategy A/B/C self-equivalence (pre-port receipt)",
            "category": "strategy-parity",
            "canonical_module": (
                "PythonDataService/app/engine/strategy/algorithms/"
                "spy_strategy_a.py, spy_strategy_b.py, spy_strategy_c.py "
                "(shared base: _rsi_range_base.py)"
            ),
            "canonical_callable": "SpyStrategyAAlgorithm, SpyStrategyBAlgorithm, SpyStrategyCAlgorithm",
            "reference": {
                "kind": "internal_regression",
                "oracle": (
                    "Strategy A/B/C's own current (pre-port) trade_log output, captured once "
                    "as a refactor-neutrality receipt — not an external correctness oracle."
                ),
                "citation": "docs/references/strategy-abc-self-equivalence.md",
            },
            "market_input": {
                "source": "synthetic",
                "vendor": None,
                "generated_by": "PythonDataService/scripts/fixture_generators/strategy_abc_self_equivalence.py",
            },
            "units": None,
            "tolerance": {
                "atol": 0.0,
                "rtol": 0.0,
                "note": (
                    "Bit-exact. All LoggedTrade fields are Decimal, int, str, or bool produced by "
                    "exact Decimal arithmetic (no float division) replayed against the same pinned "
                    "input; any divergence, even at the last Decimal digit, is exactly the signal "
                    "the S3 intent port must explain rather than silently tolerate."
                ),
            },
            "active_version": 1,
            "versions": {
                "1": {
                    "input": "input.arrow",
                    "output": "output.json",
                    "attribution": "attribution.md",
                    "content_sha256": content_hashes,
                    "file_sha256": file_hashes,
                }
            },
            "status": "active",
        }
    )
    _write_json(MANIFEST_PATH, manifest)


def _write_attribution(path: Path, trades_by_strategy: dict[str, list[LoggedTrade]]) -> None:
    counts = ", ".join(f"{key}={len(trades)}" for key, trades in trades_by_strategy.items())
    path.write_text(
        "# ENG-008 — Strategy A/B/C self-equivalence (pre-port receipt)\n\n"
        "## Evidence Layers\n\n"
        "**Layer 1 — Market input provenance:** Synthetic. A single seeded "
        f"random-walk series of {BAR_COUNT} 15-minute SPY-like bars "
        f"(`numpy.random.default_rng(seed={SEED})`, base price ${BASE_PRICE:.0f}, "
        f"per-bar step `N(0, {STEP_STD})`), shared across all three strategies. "
        "See `scripts/fixture_generators/strategy_abc_self_equivalence.py::build_bars`.\n\n"
        "**Layer 2 — Methodology provenance:** None external — this fixture exists to "
        "prove *refactor neutrality*, not mathematical correctness. Strategy A/B/C "
        "(`app/engine/strategy/algorithms/spy_strategy_{a,b,c}.py`, sharing "
        "`_rsi_range_base.py::RsiRangeStrategy`) were never ported from an outside "
        "reference (see each file's own provenance block).\n\n"
        "**Layer 3 — Independent numerical oracle:** None — `reference_kind=internal_regression`. "
        "The oracle is Strategy A/B/C's own current trade_log output, captured once at their "
        "registered default parameters. It certifies nothing about correctness; it exists "
        "so GitHub issue #1699's S3 intent port can prove numerical neutrality against it "
        "rather than argue it.\n\n"
        "## Protocol\n\n"
        "Each strategy is constructed with zero constructor overrides (registered defaults: "
        "`app/engine/strategy/registry.py` `spy_strategy_{a,b,c}` entries), run once through "
        "`BacktestEngine` against the shared bar series "
        "(`FillModel(mode=SIGNAL_BAR_CLOSE, commission_per_order=0)`), and its public "
        "`strategy.trade_log` (`list[LoggedTrade]`) is captured verbatim — not the private "
        "`_entry_extra_gate_passes` gate the existing `app/engine/tests/test_strategies_abc.py` "
        f"drives. Trade counts at generation: {counts}.\n\n"
        "## Tolerance\n\n"
        "`atol=0, rtol=0` (bit-exact). Every `LoggedTrade` field is produced by exact Decimal "
        "arithmetic (subtraction and division under a fixed `decimal` context, no float path) "
        "replayed against the same pinned Arrow input — regeneration with the recorded command "
        "reproduces the fixture exactly. See `.claude/rules/numerical-rigor.md` \"Equivalence "
        "levels\" — this is a Bit-exact receipt, not Strict float.\n\n"
        "## Regeneration\n\n"
        "  PYTHONPATH=. python scripts/fixture_generators/strategy_abc_self_equivalence.py\n\n"
        "Run from the `PythonDataService` directory. Rerun only when Strategy A/B/C's math "
        "intentionally changes (e.g. after the S3 intent port lands, to re-baseline); a change "
        "in this fixture's committed trades is itself the numerical-neutrality verdict for that "
        "port and must be justified in the commit message, per the golden-fixture lifecycle "
        "rules.\n\n"
        "## Generation Metadata\n\n"
        f"Generated: 2026-08-20\n"
        "Oracle: internal_regression — Strategy A/B/C's own current trade_log output\n"
        "Script: scripts/fixture_generators/strategy_abc_self_equivalence.py\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
