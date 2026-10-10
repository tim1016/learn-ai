"""The short-bucket consolidation fixture: LEAN's own emitted bar, replayed here (#2909).

The fixture is derived by running LEAN's ``TradeBarConsolidator`` in the
pinned sandbox image — see
``tests/fixtures/golden/lean-consolidator-short-bucket/v1/attribution.md``
for the harness and the cross-check against the vendored commit's source.
This test feeds the captured input minutes to the Python port and requires
the emitted bar to match LEAN's byte for byte: Decimal OHLC, integer
volume, and the instants as ms. Tolerance is exact (atol=0, rtol=0 per the
manifest).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from app.engine.consolidators.trade_bar_consolidator import TradeBarConsolidator
from app.engine.data.trade_bar import TradeBar
from app.utils.timestamps import to_ms_utc

_ET = ZoneInfo("America/New_York")
_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures/golden/engine-parity/lean-consolidator-short-bucket/v1"
)


def _et_ms(wall_time: str) -> int:
    naive = datetime.fromisoformat(wall_time)
    return to_ms_utc(naive.replace(tzinfo=_ET))


def test_the_port_emits_leans_short_bucket_bar_exactly() -> None:
    case = json.loads((_FIXTURE / "input.json").read_text(encoding="utf-8"))
    expected = json.loads((_FIXTURE / "output.json").read_text(encoding="utf-8"))

    consolidator = TradeBarConsolidator(
        timedelta(minutes=case["period_minutes"])
    )
    fired = None
    for row in case["minutes"]:
        price = Decimal(row["close"])
        bar = TradeBar(
            symbol="SPY",
            start_ms=_et_ms(row["time"]),
            end_ms=_et_ms(row["time"]) + 60_000,
            open=Decimal(row["open"]),
            high=Decimal(row["high"]),
            low=Decimal(row["low"]),
            close=price,
            volume=row["volume"],
        )
        fired = consolidator.update(bar)

    assert fired is not None, "the next bucket's first bar must fire the short bucket"
    assert fired.start_ms == _et_ms(expected["time"])
    # LEAN's EndTime is Time + Period: the bucket ends at its boundary even
    # though its last minute never arrived — the pin this fixture exists for.
    assert fired.end_ms == _et_ms(expected["end_time"])
    assert fired.end_ms - fired.start_ms == case["period_minutes"] * 60_000
    assert fired.open == Decimal(expected["open"])
    assert fired.high == Decimal(expected["high"])
    assert fired.low == Decimal(expected["low"])
    assert fired.close == Decimal(expected["close"])
    assert fired.volume == expected["volume"]
