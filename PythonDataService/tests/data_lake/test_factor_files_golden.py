"""Golden fixture: a dividend factor row prices raw cash on the raw close (#2479).

Oracle: QuantConnect's own published AAPL factor file, vendored at
``references/lean/7986ed0…/Data/equity/usa/factor_files/aapl.csv``. Its rows
``20200806,0.9949942,0.25,455.61`` and ``20200828,0.9967882,0.25,499.23``
encode the 2020-08-07 $0.82 dividend — paid three weeks *before* the
2020-08-31 4:1 split — as the per-event multiplier
``0.9949942 / 0.9967882 = 0.998200… = 1 − 0.82 / 455.61``: raw Polygon cash
over the raw prior close. The ToolBox ``FactorFileGenerator`` formula this
builder originally ported multiplies the cash by the cumulative later-split
factor (vendored ``ToolBox/FactorFileGenerator.cs``); that term exists for a
split-adjusted dividend feed, a basis Polygon's ``cash_amount`` does not
satisfy, and it made a later split rescale every earlier dividend day
(#2452's pinned discrepancy, ~0.135 pp per affected day).

Tolerances (``attribution.md`` in the fixture directory): exact bytes for the
built file; ``atol=1e-6, rtol=0`` reconciling against the published rows
(7-significant-digit rounding displaces their ratio by up to ~1e-7, while the
old formula sits 2.4e-3 away); ``atol=1e-9, rtol=0`` for the raw-mode
dividend cash derived below.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path

import pytest

from app.data_lake.factor_files import (
    build_factor_file_bytes,
    parse_factor_file,
    plan_factor_file,
)
from app.data_lake.polygon_corp_actions import DividendEvent, SplitEvent

_FIXTURE_DIR = (
    Path(__file__).resolve().parents[1] / "fixtures" / "golden" / "lean-factor-file-aapl"
)
_PUBLISHED_AAPL = (
    Path(__file__).resolve().parents[3]
    / "references"
    / "lean"
    / "7986ed0aade3ae5de06121682409f05984e32ff7"
    / "Data"
    / "equity"
    / "usa"
    / "factor_files"
    / "aapl.csv"
)


def _build_from_fixture() -> bytes:
    case = json.loads((_FIXTURE_DIR / "input.json").read_text())
    closes = {
        date.fromisoformat(d): Decimal(close)
        for d, close in case["regular_session_closes"].items()
    }
    plan = plan_factor_file(
        (date.fromisoformat(d) for d in case["captured_sessions"]),
        [SplitEvent(execution_date=s["execution_date"], split_from=s["split_from"], split_to=s["split_to"]) for s in case["splits"]],
        [DividendEvent(ex_dividend_date=d["ex_dividend_date"], cash_amount=float(d["cash_amount"])) for d in case["dividends"]],
    )
    return build_factor_file_bytes(case["symbol"], plan, closes)


def test_the_built_file_matches_the_reference_derived_fixture() -> None:
    assert _build_from_fixture() == (_FIXTURE_DIR / "expected_factor_file.csv").read_bytes()


def test_event_rows_reconcile_with_the_published_reference_rows() -> None:
    """The built dividend row's per-event multiplier equals the multiplier
    QuantConnect's own file encodes for the same $0.82 dividend, and the
    split row carries the same cumulative split factor (0.25) and reference
    closes (455.61 / 499.23) as the published rows."""
    built = {
        row.row_date: row for row in parse_factor_file(_build_from_fixture().decode("ascii"))
    }
    published = {
        row.row_date: row for row in parse_factor_file(_PUBLISHED_AAPL.read_text())
    }

    built_multiplier = (
        built[date(2020, 8, 6)].price_factor / built[date(2020, 8, 28)].price_factor
    )
    published_multiplier = (
        published[date(2020, 8, 6)].price_factor / published[date(2020, 8, 28)].price_factor
    )
    assert float(built_multiplier) == pytest.approx(float(published_multiplier), abs=1e-6, rel=0)
    assert built[date(2020, 8, 6)].split_factor == published[date(2020, 8, 6)].split_factor
    assert built[date(2020, 8, 28)].split_factor == published[date(2020, 8, 28)].split_factor


def test_lean_raw_mode_dividend_cash_from_the_built_rows() -> None:
    """LEAN pays ``round(ref × (1 − pf_i/pf_{i+1}), 2)`` per share
    (``CorporateFactorRow.GetDividend`` → ``Dividend.ComputeDistribution``,
    both vendored): the row dated D against the next-newer row, priced at
    D's reference close. The built rows must hand LEAN back exactly the
    $0.82 that was paid — the assertion that fails under the ported ToolBox
    term ``cash × later-split-factor``, which pays 455.61 × 0.00045 =
    $0.205 instead. The unrounded tolerance is the CSV format's 10-dp
    factor quantization, whose worst-case displacement here is
    455.61 × 1e-10 ≈ 4.6e-8; ``atol=1e-7`` leaves headroom while sitting
    three orders of magnitude below the old formula's 0.615 gap."""
    rows = {row.row_date: row for row in parse_factor_file(_build_from_fixture().decode("ascii"))}

    def distribution(row_date: date) -> Decimal:
        row = rows[row_date]
        newer = min(d for d in rows if d > row_date)
        ratio = row.price_factor / rows[newer].price_factor
        return Decimal("455.61") * (Decimal(1) - ratio)

    paid = distribution(date(2020, 8, 6))
    # LEAN's Math.Round defaults to banker's rounding; this value is not a
    # midpoint, so the mode cannot change the cents.
    assert paid.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN) == Decimal("0.82")
    assert float(paid) == pytest.approx(0.82, abs=1e-7, rel=0)
