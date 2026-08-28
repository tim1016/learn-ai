"""One canonical deci-cent encoder, pinned (carry-forward A1).

The tree carries two LEAN zip writers: the lake's
(``app.data_lake.lean_writer``) and the pre-lake policy store's
(``app.engine.data.lean_format``). Before #1839 they disagreed on how a price
finer than LEAN's 1/10,000 grid becomes the integer on disk — the lake rounded
half-up, the policy store truncated via ``int(price * 10_000)`` — so the same
Polygon bar could land one deci-cent apart depending on which writer produced
it. That divergence is exactly what the flagship parity test would have been
measuring, so it is closed here rather than tolerated there.

These tests pin the rule itself and then assert the two writers agree
bar-for-bar over prices that actually exercise it.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.data_lake.lean_writer import MinuteTradeBar, build_minute_trade_zip_bytes, to_deci_cent
from app.engine.data.lean_format import write_lean_day_zip
from app.engine.data.trade_bar import TradeBar
from app.utils.timestamps import to_ms_utc

_ET = ZoneInfo("America/New_York")


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        # On-grid prices are exact under any rounding rule.
        (Decimal("123.4567"), 1234567),
        (Decimal("0.0001"), 1),
        (Decimal("0"), 0),
        # Below the grid: half-up rounds to nearest, ties away from zero.
        (Decimal("123.45674"), 1234567),
        (Decimal("123.45675"), 1234568),
        (Decimal("123.45676"), 1234568),
        (Decimal("0.00005"), 1),
        (Decimal("0.00004"), 0),
    ],
)
def test_to_deci_cent_rounds_half_up_to_the_nearest_grid_point(price: Decimal, expected: int) -> None:
    assert to_deci_cent(price) == expected


def test_to_deci_cent_refuses_a_negative_price() -> None:
    with pytest.raises(ValueError, match="negative price"):
        to_deci_cent(Decimal("-0.01"))


def test_truncation_would_have_disagreed_on_a_sub_grid_price() -> None:
    """The divergence this consolidation closes is real, not theoretical.

    Kept as a live assertion rather than a comment: if someone reverts a
    writer to ``int(price * 10_000)``, the parity assertions below start
    failing and this test says why in one line.
    """
    price = Decimal("123.45675")
    assert int(price * Decimal(10_000)) == 1234567
    assert to_deci_cent(price) == 1234568


def _sub_grid_prices() -> list[Decimal]:
    """Prices whose 5th decimal decides the encoded integer."""
    return [
        Decimal("0.12345"),
        Decimal("0.98765"),
        Decimal("1.00005"),
        Decimal("12.345678"),
        Decimal("123.456750"),
    ]


def test_both_writers_encode_identically_on_sub_grid_prices(tmp_path) -> None:
    """The lake writer and the policy-store writer produce the same rows.

    Row-level, not byte-level: the two zips are deliberately not compared as
    bytes. The lake writer terminates its CSV with a trailing newline and the
    policy-store writer does not, so the archives differ in one byte while
    describing identical bars. Byte-equality between the two writers is
    claimed nowhere in this repo (see the T10 report's parity methodology);
    row-equality is the claim, and it is what a reader observes.
    """
    trading_date = date(2024, 5, 20)
    symbol = "SPY"
    minute_starts = [datetime(2024, 5, 20, 9, 30 + i, tzinfo=_ET) for i in range(len(_sub_grid_prices()))]

    lake_bars = [
        MinuteTradeBar(
            bar_start_et=start,
            open=price,
            high=price,
            low=price,
            close=price,
            volume=100 + index,
        )
        for index, (start, price) in enumerate(zip(minute_starts, _sub_grid_prices(), strict=True))
    ]
    store_bars = [
        TradeBar(
            symbol=symbol,
            start_ms=to_ms_utc(start),
            end_ms=to_ms_utc(start) + 60_000,
            open=price,
            high=price,
            low=price,
            close=price,
            volume=100 + index,
        )
        for index, (start, price) in enumerate(zip(minute_starts, _sub_grid_prices(), strict=True))
    ]

    lake_zip = build_minute_trade_zip_bytes(symbol, trading_date.strftime("%Y%m%d"), lake_bars)
    store_zip_path = write_lean_day_zip(tmp_path, symbol, trading_date, store_bars)

    assert _csv_rows(lake_zip) == _csv_rows(store_zip_path.read_bytes())
    # And the other half of the same claim, which only means anything here:
    # both zips describe the *same five bars*, so the one byte they differ by
    # is the lake writer's trailing newline and nothing else. This is the
    # canonical guard for "row-identical, not byte-identical" -- if a future
    # change made the writers byte-identical, this is the line that fails and
    # tells whoever made it that widening the repo's bit-exact claim from
    # "across the import" to "everywhere" is a separate decision.
    assert lake_zip != store_zip_path.read_bytes()


def _csv_rows(zip_bytes: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        body = zf.read(zf.namelist()[0]).decode("ascii")
    return [line for line in body.split("\n") if line]
