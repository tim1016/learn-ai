"""Run the EMA Previous Crossover LEAN twin over two golden cells and pin its fills (ENG-010).

The rule has no outside reference (#2902), so the oracle is LEAN's own
execution of the trusted template: LEAN consolidates the minute bars,
computes the EMAs and the RSI, and fills the orders.

Input is not copied. Each run reads the committed cross-engine capture for
its ticker, and this script refuses to write unless the minute bars LEAN
reports receiving are byte-identical to that cell's committed
``lean/observations.csv`` -- the bars the parity test replays.

LEAN pays a cash dividend on a position held over an ex-date in raw mode,
and the engine's LEAN-compatibility profile pays the same (#2903). Each
dividend LEAN paid is read with the canonical factor-file reader the engine
pays from, enriched with the shares LEAN held, and recorded beside the
fills; this script refuses to write unless fills plus dividends reproduce
the end equity LEAN reports.

Run from ``PythonDataService/`` (needs podman and the pinned LEAN image)::

    PYTHONPATH=. python tests/fixtures/golden/ema-previous-crossover-lean/generate.py
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from decimal import Decimal
from pathlib import Path

from app.engine.data.dividends import dividends_from_factor_file
from app.lean_sidecar.config import PINNED_LEAN_IMAGE_DIGEST
from app.lean_sidecar.parity_matrix.matrix import Cell, cell_by_id
from app.lean_sidecar.trusted_samples.ema_previous_crossover import EMA_PREVIOUS_CROSSOVER_SOURCE
from scripts.regenerate_cross_engine_study import _assert_lean_observations_complete, _stage_lean_run

CELL_IDS = (
    "SPY_W6mo_2025-11-03_to_2026-04-30",
    "QQQ_W6mo_2025-11-03_to_2026-04-30",
)
STARTING_CASH = Decimal("100000")
# The registered default configuration, passed explicitly so the run also
# exercises the template's parameter parsing.
STRATEGY_PARAMETERS = {
    "gap": "0.2",
    "gap_bps": "0.0",
    "rsi_min": "50.0",
    "rsi_max": "70.0",
    "fast_period": "5",
    "slow_period": "10",
    "hold_bars": "5",
    "cross_max_age_bars": "1",
}
# LEAN reports end equity to the cent.
_END_EQUITY_ATOL = Decimal("0.005")

_SERVICE_ROOT = Path(__file__).resolve().parents[4]
_STUDIES_ROOT = _SERVICE_ROOT / "tests/fixtures/golden/cross-engine-studies"
_FIXTURE_DIR = _SERVICE_ROOT / "tests/fixtures/golden/strategy-parity/ENG-010/v1"


def main() -> None:
    if PINNED_LEAN_IMAGE_DIGEST is None:
        raise RuntimeError("no LEAN image is pinned for this architecture")
    inputs: dict[str, dict[str, str]] = {}
    outputs: dict[str, dict[str, object]] = {}
    for cell_id in CELL_IDS:
        cell = cell_by_id(cell_id)
        committed = _STUDIES_ROOT / "cells" / cell_id / "lean" / "observations.csv"
        with tempfile.TemporaryDirectory() as scratch:
            run_dir = Path(scratch)
            lean_result = _stage_lean_run(
                cell,
                run_dir,
                image_digest=PINNED_LEAN_IMAGE_DIGEST,
                algorithm_source=EMA_PREVIOUS_CROSSOVER_SOURCE,
                strategy_parameters=STRATEGY_PARAMETERS,
            )
            _assert_lean_observations_complete(cell, run_dir / "lean")
            observations_sha256 = _sha256(run_dir / "lean" / "observations.csv")
            if observations_sha256 != _sha256(committed):
                raise RuntimeError(f"{cell_id}: LEAN received minute bars that differ from the committed cell's")
        fills = [
            {
                "ms_utc": event.ms_utc,
                "direction": event.direction,
                "fill_price": str(event.fill_price),
                "fill_quantity": int(event.fill_quantity),
                "order_fee": str(event.order_fee_amount),
            }
            for event in lean_result.order_events
            if event.status == "filled"
        ]
        if not fills or len(fills) % 2:
            raise RuntimeError(f"{cell_id}: expected a non-empty, paired list of LEAN fills, found {len(fills)}")
        dividends = _dividends_lean_paid(cell, fills)
        final_equity = _final_equity(fills, dividends)
        lean_end_equity = Decimal(lean_result.statistics["End Equity"])
        if abs(final_equity - lean_end_equity) > _END_EQUITY_ATOL:
            raise RuntimeError(
                f"{cell_id}: fills and dividends give {final_equity}, but LEAN reports end equity {lean_end_equity}"
            )
        # The cell id names the ticker and window; the parity matrix resolves them.
        inputs[cell_id] = {
            "observations_csv": committed.relative_to(_SERVICE_ROOT).as_posix(),
            "observations_sha256": observations_sha256,
        }
        outputs[cell_id] = {
            "filled_order_events": fills,
            "dividends": dividends,
            "final_equity": str(final_equity),
            "lean_reported_end_equity": str(lean_end_equity),
        }

    _FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    _write_json(_FIXTURE_DIR / "input.json", {"starting_cash": str(STARTING_CASH), "cells": inputs})
    _write_json(
        _FIXTURE_DIR / "output.json",
        {
            "lean_image_digest": PINNED_LEAN_IMAGE_DIGEST,
            "algorithm_source_sha256": hashlib.sha256(EMA_PREVIOUS_CROSSOVER_SOURCE.encode("utf-8")).hexdigest(),
            "parameters": STRATEGY_PARAMETERS,
            "cells": outputs,
        },
    )


def _dividends_lean_paid(cell: Cell, fills: list[dict]) -> list[dict]:
    """Every cash dividend LEAN paid in the cell: one per factor-file dividend row whose close the run held through.

    The schedule comes from the canonical factor-file reader — the same
    function the engine's LEAN-compatibility profile pays from (#2903).
    This adds the shares LEAN held through each cum-dividend close, derived
    from its fills, so the end-equity check below reproduces what LEAN
    reports.
    """
    factor_file = (
        _STUDIES_ROOT / "_lean_data_capture" / cell.ticker / "equity/usa/factor_files" / f"{cell.ticker.lower()}.csv"
    )
    dividends: list[dict] = []
    for dividend in dividends_from_factor_file(factor_file, cell.ticker, cell.start_date, cell.end_date):
        shares = sum(fill["fill_quantity"] for fill in fills if fill["ms_utc"] <= dividend.held_through_ms)
        if shares == 0:
            continue
        dividends.append(
            {
                "held_through_ms": dividend.held_through_ms,
                "per_share": str(dividend.per_share),
                "shares": shares,
                "cash": str(dividend.per_share * shares),
            }
        )
    return dividends


def _final_equity(fills: list[dict], dividends: list[dict]) -> Decimal:
    cash = STARTING_CASH + sum((Decimal(dividend["cash"]) for dividend in dividends), Decimal(0))
    for fill in fills:
        cash -= Decimal(fill["fill_quantity"]) * Decimal(fill["fill_price"])
        cash -= Decimal(fill["order_fee"])
    return cash


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
