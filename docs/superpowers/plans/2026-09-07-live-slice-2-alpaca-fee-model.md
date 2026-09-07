# Live Slice 2 — Alpaca Regulatory Fee Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A canonical, dated Alpaca equity regulatory-fee model (SEC §31, FINRA TAF, FINRA CAT) with a golden fixture and reference note, plus a predicted-vs-observed session reconciliation against the FEE activities Alpaca posts — ADR 0059 slice 2.

**Architecture:** One canonical math module in `app/broker/alpaca/regulatory_fees.py` (rate tables with effective dates, per-fill accrual at full `Decimal` precision, end-of-day per-component cent round-up). A pure reconciliation function and a thin facade in `app/services/alpaca_fee_reconciliation.py` price the session's effective SQLite fills and compare them with the day's `FEE` activities from the broker read port; `GET /api/brokers/{broker}/fees/session-reconciliation` exposes the verdict. No engine wiring and no Frontend in this slice.

**Tech Stack:** Python 3.11+, `decimal`, Pydantic v2, FastAPI, pytest (`asyncio_mode=auto`), golden-fixture manifest tooling under `tests/fixtures/golden_support/`.

**Spec:** `docs/architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md` — Decision 6 (fees) and Consequences slice 2 ("The fee model, fixture and reference note; predicted-vs-observed reconciliation"). Rate facts and their sources: `docs/references/alpaca-regulatory-fees.md` (written in Task 3 of this plan).

## Global Constraints

- **ADR 0059 D6:** observed `FEE` activities are the truth; the dated model exists to predict them and to price fills for shadow and backtest parity. Never let the model's number override an observed fee.
- **Unknown is never zero.** A component whose rate is not pinned for the trade date is `None`; a session containing an unknown cannot settle to a number (`RateNotPinnedError`). Buys owe no SEC or TAF on any date — those are `Decimal("0")` for a buy regardless of pinning.
- **Math Provenance Contract** (`.claude/skills/learn-ai-validation`): the model module carries `Formula` / `Reference` / `Canonical implementation` / `Validated against` in its docstring; one canonical implementation; the golden fixture is registered in `tests/fixtures/golden/manifest.json`; `docs/math-sources-of-truth.md` gets a row.
- **Numerical rigor:** money is `Decimal` inside the model; `float` only at the wire schema boundary. The fixture comparison is exact `Decimal` equality (`atol=0, rtol=0`); do not loosen it.
- **Temporal rigor:** every wire/storage temporal value is `int64 ms UTC` (`EpochMs` from `app.broker.alpaca.clerk.models`, `le=MAX_TIMESTAMP_MS`); the fixture stores trade dates as the ET session-open anchor (`session_open_ms_utc`), never a date string. Session structure comes only from `app/lean_sidecar/trading_calendar.py` (`is_trading_day`, `session_open_ms_utc`) and `app/utils/session_anchors.py` (`et_date_at_ms`, `et_midnight_ms`). No `time(9, 30)` literals.
- **Repo rules:** `from __future__ import annotations`; type hints on every signature; Pydantic v2 only; no `print`; no silent `except`; `ruff check PythonDataService/app/ PythonDataService/tests/` clean at project scope; tests via `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest <paths> -q`; ruff via `/Users/inkant/learn-ai/PythonDataService/.venv/bin/ruff`. Use absolute paths in every command (the shell's cwd persists between calls).
- **Contracts:** a new endpoint or schema requires `.venv/bin/python scripts/export_openapi_contract.py` (from `PythonDataService/`) and `npm run codegen:openapi` (from `Frontend/`), committed together with the code (Task 5).
- **Commits:** stage explicit paths (never `git add -A`). Every commit message ends with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- **Podman is down.** Do not run anything through `podman exec`; the host venv is the runner.

## Rate facts (pinned 2026-09-07; the plan's single source for every number below)

| Component | Applies to | Rows (effective_from → rate) | Source |
|---|---|---|---|
| SEC §31 | sells, on trade value | 2024-05-22 → $27.80/M (`0.0000278`); 2025-05-14 → $0.00/M (`0`); 2026-04-04 → $20.60/M (`0.0000206`) | SEC Fee Rate Advisories 2024-2 (2024-04-17), 2025-2 (2025-04-08), 2026-2 (2026-02-27) — `https://www.sec.gov/rules-regulations/fee-rate-advisories/<year>-2` |
| FINRA TAF | sells, per share, capped per trade | 2024-01-01 → `0.000166`/sh cap `8.30`; 2026-01-01 → `0.000195` cap `9.79`; 2027-01-01 → `0.000232` cap `11.61`; 2028-01-01 → `0.000240` cap `12.05`; 2029-01-01 → `0.000249` cap `12.50` | FINRA SR-FINRA-2024-019 fee-adjustment schedule — `https://www.finra.org/rules-guidance/rule-filings/sr-finra-2024-019/fee-adjustment-schedule`; Alpaca's schedule states the 2026 row verbatim ("$0.000195 per share, max $9.79 per trade (capped at 50,205 shares or more)") |
| FINRA CAT | buys and sells, per executed-equivalent share (NMS equity: 1 share = 1) | 2026-09-01 → `0.000003`/sh (publication date of the schedule that states it; earlier history NOT pinned) | Alpaca Securities "Broker Fee Schedule" §"Pass-Through Regulatory and Exchange Fees — Equities", `https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf`, retrieved 2026-09-07 |
| Charging | — | accrues intraday, charged at end of day, each component's total rounded **up** to $0.01 | same Alpaca schedule; `https://alpaca.markets/support/regulatory-fees` reads as per-trade round-up — the reconciliation tolerance admits both readings until live data decides (Task 4) |

Dates before a component's first row are **unpinned** for that component (SEC before 2024-05-22, TAF before 2024-01-01, CAT before 2026-09-01).

## File structure

- Create `PythonDataService/app/broker/alpaca/regulatory_fees.py` — canonical model: rate tables, `rates_for`, `fees_for_fill`, `settle_session`, `FillFees`, `SessionFees`, `RateNotPinnedError`.
- Create `PythonDataService/tests/broker/alpaca/test_regulatory_fees.py` — unit tests (regime boundaries, sides, TAF cap boundary, unpinned, settlement rounding).
- Create `PythonDataService/scripts/fixture_generators/__init__.py` (empty) and `PythonDataService/scripts/fixture_generators/alpaca_regulatory_fees.py` — hand-computed oracle generator for golden `FEE-001`.
- Create `PythonDataService/tests/fixtures/golden/broker-fees/FEE-001/v1/{input.json,output.json,attribution.md}` (generated) and register `FEE-001` in `PythonDataService/tests/fixtures/golden/manifest.json` (generated).
- Create `PythonDataService/tests/fixtures/test_alpaca_regulatory_fees_fixture.py` — golden equality test.
- Create `docs/references/alpaca-regulatory-fees.md`; modify `docs/math-sources-of-truth.md` (new section + row).
- Create `PythonDataService/app/schemas/alpaca_fee_reconciliation.py` — wire models.
- Create `PythonDataService/app/services/alpaca_fee_reconciliation.py` — `SessionFill`, `reconcile_session_fees` (pure), `session_fills` (SQLite pager), `session_fee_reconciliation` (facade).
- Create `PythonDataService/tests/services/test_alpaca_fee_reconciliation.py`.
- Modify `PythonDataService/app/routers/brokers.py` (683 lines today; +~30) — the endpoint. Create `PythonDataService/tests/routers/test_broker_fee_reconciliation.py`.
- Regenerate `contracts/` OpenAPI snapshot and `Frontend/src/app/generated/**` types (whatever `npm run codegen:openapi` rewrites).

---

### Task 1: Canonical fee model

**Files:**
- Create: `PythonDataService/app/broker/alpaca/regulatory_fees.py`
- Test: `PythonDataService/tests/broker/alpaca/test_regulatory_fees.py`

**Interfaces:**
- Consumes: `app.broker.contract.models.OrderSide` (`StrEnum`, members `BUY = "buy"`, `SELL = "sell"`).
- Produces (later tasks rely on these exact names):
  - `rates_for(trade_date: date) -> RegulatoryRates` (fields `trade_date`, `sec_per_dollar`, `taf_per_share`, `taf_cap_per_trade`, `cat_per_share`; each rate `Decimal | None`)
  - `fees_for_fill(*, trade_date: date, side: OrderSide, quantity: Decimal, fill_price: Decimal) -> FillFees` (fields `sec`, `taf`, `cat`: `Decimal | None`; property `unpinned -> tuple[str, ...]`)
  - `settle_session(fills: Sequence[FillFees]) -> SessionFees` (fields `sec`, `taf`, `cat`: `Decimal`; `fill_count: int`; property `total -> Decimal`); raises `RateNotPinnedError` (attribute `components: tuple[str, ...]`)
  - `FeeComponent = Literal["sec", "taf", "cat"]`

- [ ] **Step 1: Write the failing tests**

Create `PythonDataService/tests/broker/alpaca/test_regulatory_fees.py`:

```python
"""Unit tests for the canonical Alpaca regulatory fee model (ADR 0059 D6)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.broker.alpaca.regulatory_fees import (
    FillFees,
    RateNotPinnedError,
    fees_for_fill,
    rates_for,
    settle_session,
)
from app.broker.contract.models import OrderSide

D = Decimal
TRADE_DATE_2026 = date(2026, 9, 8)


@pytest.mark.parametrize(
    ("trade_date", "sec_per_dollar"),
    [
        (date(2024, 5, 21), None),  # before the first pinned SEC row
        (date(2024, 5, 22), D("0.0000278")),  # SEC advisory 2024-2
        (date(2025, 5, 13), D("0.0000278")),
        (date(2025, 5, 14), D("0")),  # SEC advisory 2025-2: $0.00 per million
        (date(2026, 4, 3), D("0")),
        (date(2026, 4, 4), D("0.0000206")),  # SEC advisory 2026-2
    ],
)
def test_rates_for_sec_regime_boundaries(trade_date: date, sec_per_dollar: Decimal | None) -> None:
    assert rates_for(trade_date).sec_per_dollar == sec_per_dollar


@pytest.mark.parametrize(
    ("trade_date", "per_share", "cap"),
    [
        (date(2023, 12, 31), None, None),  # before the first pinned TAF row
        (date(2024, 1, 1), D("0.000166"), D("8.30")),
        (date(2025, 12, 31), D("0.000166"), D("8.30")),
        (date(2026, 1, 1), D("0.000195"), D("9.79")),
        (date(2027, 1, 1), D("0.000232"), D("11.61")),
        (date(2028, 1, 1), D("0.000240"), D("12.05")),
        (date(2029, 1, 1), D("0.000249"), D("12.50")),
    ],
)
def test_rates_for_taf_annual_schedule(trade_date: date, per_share: Decimal | None, cap: Decimal | None) -> None:
    rates = rates_for(trade_date)
    assert rates.taf_per_share == per_share
    assert rates.taf_cap_per_trade == cap


@pytest.mark.parametrize(
    ("trade_date", "cat_per_share"),
    [(date(2026, 8, 31), None), (date(2026, 9, 1), D("0.000003"))],
)
def test_rates_for_cat_pinned_from_publication(trade_date: date, cat_per_share: Decimal | None) -> None:
    assert rates_for(trade_date).cat_per_share == cat_per_share


def test_sell_accrues_all_three_components_unrounded() -> None:
    fees = fees_for_fill(trade_date=TRADE_DATE_2026, side=OrderSide.SELL, quantity=D("100"), fill_price=D("250.00"))

    assert fees == FillFees(sec=D("0.515"), taf=D("0.0195"), cat=D("0.0003"))
    assert fees.unpinned == ()


def test_buy_owes_only_cat() -> None:
    fees = fees_for_fill(trade_date=TRADE_DATE_2026, side=OrderSide.BUY, quantity=D("100"), fill_price=D("250.00"))

    assert fees == FillFees(sec=D("0"), taf=D("0"), cat=D("0.0003"))


@pytest.mark.parametrize(
    ("quantity", "expected_taf"),
    [
        (D("50205"), D("9.789975")),  # 50,205 × 0.000195 is still under the cap
        (D("50206"), D("9.79")),  # one more share and the per-trade cap binds
        (D("60000"), D("9.79")),
    ],
)
def test_taf_cap_binds_above_boundary(quantity: Decimal, expected_taf: Decimal) -> None:
    fees = fees_for_fill(trade_date=TRADE_DATE_2026, side=OrderSide.SELL, quantity=quantity, fill_price=D("1.00"))

    assert fees.taf == expected_taf


def test_fractional_share_accrues_proportionally() -> None:
    fees = fees_for_fill(trade_date=TRADE_DATE_2026, side=OrderSide.BUY, quantity=D("0.5"), fill_price=D("400.00"))

    assert fees.cat == D("0.0000015")


def test_unpinned_component_is_none_not_zero() -> None:
    fees = fees_for_fill(trade_date=date(2025, 6, 2), side=OrderSide.SELL, quantity=D("100"), fill_price=D("250.00"))

    assert fees.sec == D("0")  # the $0.00 regime is a pinned zero
    assert fees.taf == D("0.0166")
    assert fees.cat is None
    assert fees.unpinned == ("cat",)


def test_buy_before_sec_pin_still_owes_zero_sec_and_taf() -> None:
    fees = fees_for_fill(trade_date=date(2024, 3, 1), side=OrderSide.BUY, quantity=D("10"), fill_price=D("100.00"))

    assert (fees.sec, fees.taf, fees.cat) == (D("0"), D("0"), None)


def test_sell_before_sec_pin_has_unpinned_sec() -> None:
    fees = fees_for_fill(trade_date=date(2024, 3, 1), side=OrderSide.SELL, quantity=D("10"), fill_price=D("100.00"))

    assert fees.sec is None
    assert fees.taf == D("0.00166")
    assert fees.unpinned == ("sec", "cat")


@pytest.mark.parametrize("quantity", [D("0"), D("-1")])
def test_non_positive_quantity_is_refused(quantity: Decimal) -> None:
    with pytest.raises(ValueError, match="positive"):
        fees_for_fill(trade_date=TRADE_DATE_2026, side=OrderSide.SELL, quantity=quantity, fill_price=D("1"))


def test_settle_session_rounds_each_component_up_to_the_cent() -> None:
    fills = [
        fees_for_fill(trade_date=TRADE_DATE_2026, side=OrderSide.SELL, quantity=D("100"), fill_price=D("250.00")),
        fees_for_fill(trade_date=TRADE_DATE_2026, side=OrderSide.BUY, quantity=D("100"), fill_price=D("250.00")),
        fees_for_fill(trade_date=TRADE_DATE_2026, side=OrderSide.SELL, quantity=D("60000"), fill_price=D("10.00")),
        fees_for_fill(trade_date=TRADE_DATE_2026, side=OrderSide.SELL, quantity=D("50205"), fill_price=D("1.00")),
        fees_for_fill(trade_date=TRADE_DATE_2026, side=OrderSide.SELL, quantity=D("50206"), fill_price=D("1.00")),
        fees_for_fill(trade_date=TRADE_DATE_2026, side=OrderSide.BUY, quantity=D("0.5"), fill_price=D("400.00")),
    ]

    settled = settle_session(fills)

    # sec: 0.515 + 12.36 + 1.034223 + 1.0342436 = 14.9434666 → 14.95
    # taf: 0.0195 + 9.79 + 9.789975 + 9.79     = 29.389475  → 29.39
    # cat: 0.0003·2 + 0.18 + 0.150615 + 0.150618 + 0.0000015 = 0.4818345 → 0.49
    assert (settled.sec, settled.taf, settled.cat) == (D("14.95"), D("29.39"), D("0.49"))
    assert settled.total == D("44.83")
    assert settled.fill_count == 6


def test_settle_session_does_not_bump_an_exact_cent() -> None:
    settled = settle_session([FillFees(sec=D("0.50"), taf=D("0"), cat=D("0.010"))])

    assert (settled.sec, settled.taf, settled.cat) == (D("0.50"), D("0.00"), D("0.01"))


def test_settle_session_of_nothing_is_zero() -> None:
    settled = settle_session([])

    assert settled.total == D("0.00")
    assert settled.fill_count == 0


def test_settle_session_refuses_an_unpinned_component() -> None:
    fills = [fees_for_fill(trade_date=date(2025, 6, 2), side=OrderSide.SELL, quantity=D("1"), fill_price=D("1"))]

    with pytest.raises(RateNotPinnedError) as excinfo:
        settle_session(fills)

    assert excinfo.value.components == ("cat",)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/test_regulatory_fees.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'app.broker.alpaca.regulatory_fees'`.

- [ ] **Step 3: Write the model**

Create `PythonDataService/app/broker/alpaca/regulatory_fees.py`:

```python
"""Alpaca equity regulatory pass-through fees (ADR 0059 D6).

Formula:
    value = quantity × fill_price
    SEC §31 (sells only)      sec = value × r_sec(d)
    FINRA TAF (sells only)    taf = min(quantity × r_taf(d), cap_taf(d))
    FINRA CAT (both sides)    cat = quantity × r_cat(d)   (NMS equity: 1 share = 1 EES)
    Session settlement        each component summed over the ET trade date and
                              rounded UP to the cent; total = sec + taf + cat
Reference:
    Alpaca Securities LLC, "Broker Fee Schedule", §"Pass-Through Regulatory and
    Exchange Fees — Equities"
    (https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf, retrieved
    2026-09-07); SEC Fee Rate Advisories 2024-2, 2025-2, 2026-2
    (https://www.sec.gov/rules-regulations/fee-rate-advisories/<year>-2); FINRA
    SR-FINRA-2024-019 fee-adjustment schedule
    (https://www.finra.org/rules-guidance/rule-filings/sr-finra-2024-019/fee-adjustment-schedule).
    Row-by-row citations: docs/references/alpaca-regulatory-fees.md.
Canonical implementation: this file.
Validated against:
    tests/broker/alpaca/test_regulatory_fees.py;
    tests/fixtures/test_alpaca_regulatory_fees_fixture.py (golden FEE-001).

Observed FEE activities are the truth (ADR 0059 D6); this model predicts them
and prices fills for shadow and backtest parity. A component whose rate is not
pinned for the trade date is ``None`` — never zero — so "unknown" can never be
read as "free". Buys owe no SEC or TAF on any date, so those are ``0`` for a
buy regardless of pinning. Only listed (NMS) equities are modelled: one share
is one CAT executed-equivalent share.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, Decimal
from typing import Literal

from app.broker.contract.models import OrderSide

FeeComponent = Literal["sec", "taf", "cat"]

_COMPONENTS: tuple[FeeComponent, ...] = ("sec", "taf", "cat")
_CENT = Decimal("0.01")
_ZERO = Decimal("0")

# Each table is ascending by effective date; the latest row on or before the
# trade date is in force. A trade date before a table's first row has NO pinned
# rate for that component. Row sources: docs/references/alpaca-regulatory-fees.md.
_SEC_PER_DOLLAR: tuple[tuple[date, Decimal], ...] = (
    (date(2024, 5, 22), Decimal("0.0000278")),  # $27.80 per $1M — SEC advisory 2024-2
    (date(2025, 5, 14), Decimal("0")),  # $0.00 per $1M — SEC advisory 2025-2
    (date(2026, 4, 4), Decimal("0.0000206")),  # $20.60 per $1M — SEC advisory 2026-2
)
_TAF_PER_SHARE_AND_CAP: tuple[tuple[date, Decimal, Decimal], ...] = (
    (date(2024, 1, 1), Decimal("0.000166"), Decimal("8.30")),
    (date(2026, 1, 1), Decimal("0.000195"), Decimal("9.79")),
    (date(2027, 1, 1), Decimal("0.000232"), Decimal("11.61")),
    (date(2028, 1, 1), Decimal("0.000240"), Decimal("12.05")),
    (date(2029, 1, 1), Decimal("0.000249"), Decimal("12.50")),
)
_CAT_PER_SHARE: tuple[tuple[date, Decimal], ...] = (
    (date(2026, 9, 1), Decimal("0.000003")),  # Alpaca fee schedule, retrieved 2026-09-07
)


class RateNotPinnedError(ValueError):
    """A session holds a fill with no pinned rate for some component."""

    def __init__(self, components: Sequence[FeeComponent]) -> None:
        self.components: tuple[FeeComponent, ...] = tuple(components)
        super().__init__(f"no pinned regulatory rate for: {', '.join(self.components)}")


@dataclass(frozen=True)
class RegulatoryRates:
    """Rates in force on one trade date; ``None`` means not pinned for that date."""

    trade_date: date
    sec_per_dollar: Decimal | None
    taf_per_share: Decimal | None
    taf_cap_per_trade: Decimal | None
    cat_per_share: Decimal | None


@dataclass(frozen=True)
class FillFees:
    """Unrounded per-fill accruals; a ``None`` component is unpinned, not zero."""

    sec: Decimal | None
    taf: Decimal | None
    cat: Decimal | None

    @property
    def unpinned(self) -> tuple[FeeComponent, ...]:
        return tuple(name for name in _COMPONENTS if getattr(self, name) is None)


@dataclass(frozen=True)
class SessionFees:
    """What Alpaca charges at end of day: each component's accrual rounded up to the cent."""

    sec: Decimal
    taf: Decimal
    cat: Decimal
    fill_count: int

    @property
    def total(self) -> Decimal:
        return self.sec + self.taf + self.cat


def _pinned_row(table: Sequence[tuple], trade_date: date) -> tuple | None:
    """Latest row whose effective date is on or before ``trade_date``, else ``None``."""
    row = None
    for candidate in table:
        if candidate[0] <= trade_date:
            row = candidate
    return row


def rates_for(trade_date: date) -> RegulatoryRates:
    """Resolve the three pass-through rates in force on ``trade_date``."""
    sec = _pinned_row(_SEC_PER_DOLLAR, trade_date)
    taf = _pinned_row(_TAF_PER_SHARE_AND_CAP, trade_date)
    cat = _pinned_row(_CAT_PER_SHARE, trade_date)
    return RegulatoryRates(
        trade_date=trade_date,
        sec_per_dollar=None if sec is None else sec[1],
        taf_per_share=None if taf is None else taf[1],
        taf_cap_per_trade=None if taf is None else taf[2],
        cat_per_share=None if cat is None else cat[1],
    )


def fees_for_fill(
    *,
    trade_date: date,
    side: OrderSide,
    quantity: Decimal,
    fill_price: Decimal,
) -> FillFees:
    """Accrue one fill's fees at full precision; rounding happens at settlement."""
    if quantity <= 0 or fill_price <= 0:
        raise ValueError(
            f"fill quantity and price must be positive; got quantity={quantity} fill_price={fill_price}"
        )
    rates = rates_for(trade_date)
    cat = None if rates.cat_per_share is None else quantity * rates.cat_per_share
    if side != OrderSide.SELL:
        return FillFees(sec=_ZERO, taf=_ZERO, cat=cat)
    sec = None if rates.sec_per_dollar is None else quantity * fill_price * rates.sec_per_dollar
    taf = None
    if rates.taf_per_share is not None and rates.taf_cap_per_trade is not None:
        taf = min(quantity * rates.taf_per_share, rates.taf_cap_per_trade)
    return FillFees(sec=sec, taf=taf, cat=cat)


def _ceil_cents(amount: Decimal) -> Decimal:
    return amount.quantize(_CENT, rounding=ROUND_CEILING)


def settle_session(fills: Sequence[FillFees]) -> SessionFees:
    """Sum each component over one ET trade date and round it UP to the cent.

    Raises ``RateNotPinnedError`` if any fill has an unpinned component: a
    session containing an unknown cannot settle to a number.
    """
    unpinned = sorted({name for fill in fills for name in fill.unpinned})
    if unpinned:
        raise RateNotPinnedError(unpinned)
    return SessionFees(
        sec=_ceil_cents(sum((fill.sec for fill in fills), _ZERO)),
        taf=_ceil_cents(sum((fill.taf for fill in fills), _ZERO)),
        cat=_ceil_cents(sum((fill.cat for fill in fills), _ZERO)),
        fill_count=len(fills),
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/test_regulatory_fees.py -q`
Expected: all pass (30 tests: 20 parametrized cases and 10 single tests).

- [ ] **Step 5: Lint and commit**

Run: `/Users/inkant/learn-ai/PythonDataService/.venv/bin/ruff check /Users/inkant/learn-ai/PythonDataService/app/ /Users/inkant/learn-ai/PythonDataService/tests/` — expected: no findings (fix any ruff finding in the two new files; do not touch other files).

```bash
cd /Users/inkant/learn-ai && git add PythonDataService/app/broker/alpaca/regulatory_fees.py PythonDataService/tests/broker/alpaca/test_regulatory_fees.py && git commit -m "feat(broker): canonical Alpaca regulatory fee model with dated SEC/TAF/CAT rates (ADR 0059 D6)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Golden fixture FEE-001

**Files:**
- Create: `PythonDataService/scripts/fixture_generators/__init__.py` (empty file — makes the generator runnable with `-m` so it can import the calendar module)
- Create: `PythonDataService/scripts/fixture_generators/alpaca_regulatory_fees.py`
- Generated: `PythonDataService/tests/fixtures/golden/broker-fees/FEE-001/v1/{input.json,output.json,attribution.md}`; entry appended to `PythonDataService/tests/fixtures/golden/manifest.json`
- Test: `PythonDataService/tests/fixtures/test_alpaca_regulatory_fees_fixture.py`

**Interfaces:**
- Consumes: Task 1's `fees_for_fill`, `settle_session`, `FillFees`; `app.lean_sidecar.trading_calendar.session_open_ms_utc(d: date) -> int`; `app.utils.session_anchors.et_date_at_ms(ms: int) -> date`.
- Produces: fixture id `FEE-001`, category `broker-fees`, files under `tests/fixtures/golden/broker-fees/FEE-001/v1/`.

The oracle is the generator's own `Decimal` arithmetic over literal rates; it never imports `app.broker.alpaca.regulatory_fees`. Expected values (verify by hand — they also appear in Task 1's settlement test):

| case | trade date | side | qty | price | sec | taf | cat |
|---|---|---|---|---|---|---|---|
| `sell_2026` | 2026-09-08 | sell | 100 | 250.00 | 0.515 | 0.0195 | 0.0003 |
| `buy_2026` | 2026-09-08 | buy | 100 | 250.00 | 0 | 0 | 0.0003 |
| `sell_above_taf_cap` | 2026-09-08 | sell | 60000 | 10.00 | 12.36 | 9.79 | 0.18 |
| `sell_at_taf_cap_boundary` | 2026-09-08 | sell | 50205 | 1.00 | 1.034223 | 9.789975 | 0.150615 |
| `sell_just_over_taf_cap` | 2026-09-08 | sell | 50206 | 1.00 | 1.0342436 | 9.79 | 0.150618 |
| `fractional_buy` | 2026-09-08 | buy | 0.5 | 400.00 | 0 | 0 | 0.0000015 |
| `sell_sec_zero_regime_2025` | 2025-06-02 | sell | 100 | 250.00 | 0 | 0.0166 | null |
| `sell_sec_2780_regime_2024` | 2024-06-03 | sell | 1000 | 50.00 | 1.39 | 0.166 | null |
| `sell_before_sec_pin_2024` | 2024-03-01 | sell | 10 | 100.00 | null | 0.00166 | null |
| `buy_before_cat_pin_2025` | 2025-06-02 | buy | 10 | 100.00 | 0 | 0 | null |

Session (the six 2026-09-08 cases): sec `14.95`, taf `29.39`, cat `0.49`, total `44.83`. All four trade dates are NYSE trading days (2026-09-07 is Labor Day; the 8th is a Tuesday).

- [ ] **Step 1: Write the failing fixture test**

Create `PythonDataService/tests/fixtures/test_alpaca_regulatory_fees_fixture.py`:

```python
"""Golden FEE-001: the canonical fee model reproduces the hand-computed oracle exactly."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from app.broker.alpaca.regulatory_fees import fees_for_fill, settle_session
from app.broker.contract.models import OrderSide
from app.utils.session_anchors import et_date_at_ms

FIXTURE_DIR = Path(__file__).parent / "golden" / "broker-fees" / "FEE-001" / "v1"


def _load() -> tuple[dict, dict]:
    fixture_input = json.loads((FIXTURE_DIR / "input.json").read_text(encoding="utf-8"))
    fixture_output = json.loads((FIXTURE_DIR / "output.json").read_text(encoding="utf-8"))
    return fixture_input, fixture_output


def _decimal_or_none(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _price(case: dict):
    return fees_for_fill(
        trade_date=et_date_at_ms(case["trade_date_ms"]),
        side=OrderSide(case["side"]),
        quantity=Decimal(case["quantity"]),
        fill_price=Decimal(case["fill_price"]),
    )


def test_fee_001_per_fill_accruals_match_oracle_exactly() -> None:
    fixture_input, fixture_output = _load()

    for case in fixture_input["fills"]:
        fees = _price(case)
        expected = fixture_output["fills"][case["case"]]
        # Exact Decimal equality — atol=0, rtol=0 (manifest FEE-001).
        assert fees.sec == _decimal_or_none(expected["sec"]), case["case"]
        assert fees.taf == _decimal_or_none(expected["taf"]), case["case"]
        assert fees.cat == _decimal_or_none(expected["cat"]), case["case"]


def test_fee_001_session_settlement_matches_oracle_exactly() -> None:
    fixture_input, fixture_output = _load()
    by_case = {case["case"]: case for case in fixture_input["fills"]}

    settled = settle_session([_price(by_case[name]) for name in fixture_input["session"]["cases"]])

    expected = fixture_output["session"]
    assert (settled.sec, settled.taf, settled.cat) == (
        Decimal(expected["sec"]),
        Decimal(expected["taf"]),
        Decimal(expected["cat"]),
    )
    assert settled.total == Decimal(expected["total"]) == Decimal("44.83")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/fixtures/test_alpaca_regulatory_fees_fixture.py -q`
Expected: FAIL with `FileNotFoundError` for `input.json`.

- [ ] **Step 3: Write the generator**

Create an empty `PythonDataService/scripts/fixture_generators/__init__.py`, then create `PythonDataService/scripts/fixture_generators/alpaca_regulatory_fees.py`:

```python
"""Generate the FEE-001 Alpaca regulatory-fee golden fixture.

The oracle is hand-computed decimal arithmetic over the published rates. It
never imports the canonical implementation. Run from ``PythonDataService/``:

    .venv/bin/python -m scripts.fixture_generators.alpaca_regulatory_fees
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

from app.lean_sidecar.trading_calendar import session_open_ms_utc

FIXTURE_ID = "FEE-001"
ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "golden" / "broker-fees" / FIXTURE_ID / "v1"
MANIFEST_PATH = ROOT / "tests" / "fixtures" / "golden" / "manifest.json"

# Published rates in force on each case date (docs/references/alpaca-regulatory-fees.md).
# ``None`` = not pinned for that date; the oracle emits null and the model must too.
_RATES: dict[date, dict[str, Decimal | None]] = {
    date(2026, 9, 8): {
        "sec": Decimal("0.0000206"),
        "taf": Decimal("0.000195"),
        "taf_cap": Decimal("9.79"),
        "cat": Decimal("0.000003"),
    },
    date(2025, 6, 2): {"sec": Decimal("0"), "taf": Decimal("0.000166"), "taf_cap": Decimal("8.30"), "cat": None},
    date(2024, 6, 3): {
        "sec": Decimal("0.0000278"),
        "taf": Decimal("0.000166"),
        "taf_cap": Decimal("8.30"),
        "cat": None,
    },
    date(2024, 3, 1): {"sec": None, "taf": Decimal("0.000166"), "taf_cap": Decimal("8.30"), "cat": None},
}

# (case, trade date, side, quantity, fill price)
_CASES: tuple[tuple[str, date, str, str, str], ...] = (
    ("sell_2026", date(2026, 9, 8), "sell", "100", "250.00"),
    ("buy_2026", date(2026, 9, 8), "buy", "100", "250.00"),
    ("sell_above_taf_cap", date(2026, 9, 8), "sell", "60000", "10.00"),
    ("sell_at_taf_cap_boundary", date(2026, 9, 8), "sell", "50205", "1.00"),
    ("sell_just_over_taf_cap", date(2026, 9, 8), "sell", "50206", "1.00"),
    ("fractional_buy", date(2026, 9, 8), "buy", "0.5", "400.00"),
    ("sell_sec_zero_regime_2025", date(2025, 6, 2), "sell", "100", "250.00"),
    ("sell_sec_2780_regime_2024", date(2024, 6, 3), "sell", "1000", "50.00"),
    ("sell_before_sec_pin_2024", date(2024, 3, 1), "sell", "10", "100.00"),
    ("buy_before_cat_pin_2025", date(2025, 6, 2), "buy", "10", "100.00"),
)
_SESSION_CASES = tuple(name for name, day, *_ in _CASES if day == date(2026, 9, 8))


def _oracle(day: date, side: str, quantity: Decimal, price: Decimal) -> dict[str, Decimal | None]:
    rates = _RATES[day]
    cat = None if rates["cat"] is None else quantity * rates["cat"]
    if side == "buy":
        return {"sec": Decimal("0"), "taf": Decimal("0"), "cat": cat}
    sec = None if rates["sec"] is None else quantity * price * rates["sec"]
    taf = None if rates["taf"] is None else min(quantity * rates["taf"], rates["taf_cap"])
    return {"sec": sec, "taf": taf, "cat": cat}


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _ceil_cents(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _hashes(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    file_hash = hashlib.sha256(raw).hexdigest()
    normalized = json.dumps(
        json.loads(raw.decode("utf-8")),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest(), file_hash


def main() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    input_path = FIXTURE_DIR / "input.json"
    output_path = FIXTURE_DIR / "output.json"
    attribution_path = FIXTURE_DIR / "attribution.md"

    fills_in: list[dict[str, Any]] = []
    fills_out: dict[str, dict[str, str | None]] = {}
    session_sums = {"sec": Decimal("0"), "taf": Decimal("0"), "cat": Decimal("0")}
    for name, day, side, quantity_text, price_text in _CASES:
        quantity, price = Decimal(quantity_text), Decimal(price_text)
        fees = _oracle(day, side, quantity, price)
        fills_in.append(
            {
                "case": name,
                "trade_date_ms": session_open_ms_utc(day),
                "side": side,
                "quantity": quantity_text,
                "fill_price": price_text,
            }
        )
        fills_out[name] = {key: _text(value) for key, value in fees.items()}
        if name in _SESSION_CASES:
            for key in session_sums:
                session_sums[key] += fees[key]  # every 2026-09-08 component is pinned
    settled = {key: _ceil_cents(value) for key, value in session_sums.items()}

    _write_json(
        input_path,
        {
            "fills": fills_in,
            "session": {"trade_date_ms": session_open_ms_utc(date(2026, 9, 8)), "cases": list(_SESSION_CASES)},
        },
    )
    _write_json(
        output_path,
        {
            "fills": fills_out,
            "session": {
                "sec": str(settled["sec"]),
                "taf": str(settled["taf"]),
                "cat": str(settled["cat"]),
                "total": str(sum(settled.values(), Decimal("0"))),
            },
        },
    )
    attribution_path.write_text(
        "# FEE-001 — Alpaca equity regulatory pass-through fees\n\n"
        "## Source\n\n"
        "Rates are the published pass-through schedule pinned on 2026-09-07: Alpaca "
        "Securities \"Broker Fee Schedule\" §\"Pass-Through Regulatory and Exchange Fees — "
        "Equities\" (https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf), SEC "
        "Fee Rate Advisories 2024-2 / 2025-2 / 2026-2, and the FINRA SR-FINRA-2024-019 TAF "
        "fee-adjustment schedule. Row-by-row citations: docs/references/alpaca-regulatory-fees.md.\n\n"
        "## Independent numerical oracle\n\n"
        "`reference_kind=hand_computed`. Expected values are exact decimal arithmetic over the "
        "literal rates in this generator, computed without importing "
        "`app.broker.alpaca.regulatory_fees`. Sells: `sec = qty × price × r_sec`, "
        "`taf = min(qty × r_taf, cap)`, `cat = qty × r_cat`; buys owe only CAT. A component "
        "with no pinned rate on the case date is `null`, never zero.\n\n"
        "## Cases\n\n"
        "- Six 2026-09-08 fills (SEC $20.60/M, TAF $0.000195 cap $9.79, CAT $0.000003) including "
        "the TAF cap boundary: 50,205 shares → `9.789975` (under the cap), 50,206 → `9.79` (capped).\n"
        "- `sell_sec_zero_regime_2025` (2025-06-02): SEC `0` is a pinned zero; CAT unpinned → null.\n"
        "- `sell_sec_2780_regime_2024` (2024-06-03): SEC $27.80/M → `1.39` on $50,000.\n"
        "- `sell_before_sec_pin_2024` (2024-03-01): SEC unpinned → null; TAF pinned at 2024 rates.\n"
        "- Session settlement over the six 2026-09-08 fills: each component summed then rounded "
        "UP to the cent — sec `14.9434666 → 14.95`, taf `29.389475 → 29.39`, cat "
        "`0.4818345 → 0.49`, total `44.83`.\n\n"
        "## Timestamps\n\n"
        "`trade_date_ms` is the ET session-open anchor of the trade date "
        "(`session_open_ms_utc`), `int64 ms UTC`; the model derives the ET date with "
        "`et_date_at_ms`.\n\n"
        "## Tolerance\n\n"
        "`atol=0, rtol=0`: the model and the oracle are both exact `Decimal` arithmetic; the "
        "test compares `Decimal` values for equality.\n\n"
        "## Regeneration\n\n"
        "`cd PythonDataService && .venv/bin/python -m scripts.fixture_generators.alpaca_regulatory_fees`\n",
        encoding="utf-8",
    )

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest["fixtures"] = [item for item in manifest["fixtures"] if item["id"] != FIXTURE_ID]
    content_hashes: dict[str, str] = {}
    file_hashes: dict[str, str] = {}
    for path in (input_path, output_path):
        content_hash, file_hash = _hashes(path)
        content_hashes[path.name] = content_hash
        file_hashes[path.name] = file_hash
    file_hashes[attribution_path.name] = hashlib.sha256(attribution_path.read_bytes()).hexdigest()
    manifest["fixtures"].append(
        {
            "id": FIXTURE_ID,
            "name": "Alpaca equity regulatory pass-through fees",
            "category": "broker-fees",
            "canonical_module": "PythonDataService/app/broker/alpaca/regulatory_fees.py",
            "canonical_callable": "fees_for_fill",
            "reference": {
                "kind": "hand_computed",
                "oracle": "exact decimal arithmetic over the published SEC/TAF/CAT rates without importing canonical code",
                "citation": "Alpaca Broker Fee Schedule (retrieved 2026-09-07), SEC fee-rate advisories 2024-2/2025-2/2026-2, FINRA SR-FINRA-2024-019; see docs/references/alpaca-regulatory-fees.md.",
            },
            "market_input": {
                "source": "synthetic",
                "vendor": None,
                "generated_by": "PythonDataService/scripts/fixture_generators/alpaca_regulatory_fees.py",
            },
            "units": None,
            "tolerance": {
                "atol": 0.0,
                "rtol": 0.0,
                "note": "Exact Decimal oracle and exact Decimal model; compared for Decimal equality.",
            },
            "active_version": 1,
            "versions": {
                "1": {
                    "input": "input.json",
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


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Generate the fixture and run the tests**

Run: `cd /Users/inkant/learn-ai/PythonDataService && .venv/bin/python -m scripts.fixture_generators.alpaca_regulatory_fees && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/fixtures/test_alpaca_regulatory_fees_fixture.py tests/fixtures/test_golden_manifest.py -q`
Expected: the three fixture files exist, `manifest.json` gains the `FEE-001` entry, all tests pass. Open `output.json` and confirm `session.total` is `"44.83"` and `fills.sell_at_taf_cap_boundary.taf` is `"9.789975"`. If the manifest test rejects the entry (schema), fix the generator's entry — never hand-edit `manifest.json` or the fixture files.

- [ ] **Step 5: Lint and commit**

Run ruff at project scope (command in Global Constraints) — expected clean.

```bash
cd /Users/inkant/learn-ai && git add PythonDataService/scripts/fixture_generators/__init__.py PythonDataService/scripts/fixture_generators/alpaca_regulatory_fees.py PythonDataService/tests/fixtures/golden/broker-fees/FEE-001/v1 PythonDataService/tests/fixtures/golden/manifest.json PythonDataService/tests/fixtures/test_alpaca_regulatory_fees_fixture.py && git commit -m "test(fixtures): golden FEE-001 pins the Alpaca regulatory fee model to a hand-computed oracle

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Reference note and registry row

**Files:**
- Create: `docs/references/alpaca-regulatory-fees.md`
- Modify: `docs/math-sources-of-truth.md` — add a section `### Broker fees (pass-through)` immediately after the `### Broker display read models` section's table (before `### Indicators — Python-canonical, ported from LEAN`), with the registry table header and one row.

**Interfaces:**
- Consumes: Task 1's module and Task 2's fixture id. No code.

- [ ] **Step 1: Write the reference note**

Create `docs/references/alpaca-regulatory-fees.md` with exactly this content (fill nothing in later — every number is final):

```markdown
# Alpaca equity regulatory pass-through fees

## Canonical contract

`PythonDataService/app/broker/alpaca/regulatory_fees.py` prices the three regulatory
pass-throughs Alpaca charges on US listed equities and settles them the way Alpaca
charges them. ADR 0059 D6 fixes the authority: **observed `FEE` activities are the
truth**; this model predicts them (reconciliation) and prices fills where no
observation exists yet (shadow, backtest parity).

- Sells: `sec = quantity × fill_price × r_sec(d)`; `taf = min(quantity × r_taf(d), cap_taf(d))`.
- Buys and sells: `cat = quantity × r_cat(d)` (NMS equity: one share is one executed-equivalent share).
- Settlement: each component summed over the ET trade date, rounded **up** to the cent; the
  charge is the sum of the three rounded components.
- A component with no pinned rate on `d` is `None`, never `0`; a session containing one
  raises `RateNotPinnedError`. Buys owe no SEC or TAF on any date, so those are `0`
  for buys regardless of pinning.

## Rate table (pinned 2026-09-07)

| Component | Effective from | Rate | Source |
|---|---|---|---|
| SEC §31 (sells, on value) | 2024-05-22 | $27.80 per $1M (`0.0000278`) | SEC Fee Rate Advisory 2024-2, 2024-04-17 — https://www.sec.gov/rules-regulations/fee-rate-advisories/2024-2 |
| SEC §31 | 2025-05-14 | $0.00 per $1M (`0`) | SEC Fee Rate Advisory 2025-2, 2025-04-08 — https://www.sec.gov/rules-regulations/fee-rate-advisories/2025-2 |
| SEC §31 | 2026-04-04 | $20.60 per $1M (`0.0000206`) | SEC Fee Rate Advisory 2026-2, 2026-02-27 — https://www.sec.gov/rules-regulations/fee-rate-advisories/2026-2 |
| FINRA TAF (sells, per share, per-trade cap) | 2024-01-01 | `0.000166` / share, cap `8.30` | FINRA SR-FINRA-2024-019 fee-adjustment schedule — https://www.finra.org/rules-guidance/rule-filings/sr-finra-2024-019/fee-adjustment-schedule |
| FINRA TAF | 2026-01-01 | `0.000195` / share, cap `9.79` (binds at 50,206+ shares) | same; stated verbatim by Alpaca's schedule |
| FINRA TAF | 2027-01-01 | `0.000232` / share, cap `11.61` | same |
| FINRA TAF | 2028-01-01 | `0.000240` / share, cap `12.05` | same |
| FINRA TAF | 2029-01-01 | `0.000249` / share, cap `12.50` | same |
| FINRA CAT (both sides, per executed-equivalent share) | 2026-09-01 | `0.000003` / share | Alpaca Securities "Broker Fee Schedule", §"Pass-Through Regulatory and Exchange Fees — Equities" — https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf (retrieved 2026-09-07; the schedule is dated 2026-09-01) |

Unpinned windows (the model returns `None` there): SEC before 2024-05-22 (the FY2023 $8.00/M
rate's start is not pinned), TAF before 2024-01-01, CAT before 2026-09-01 (CAT's earlier
rate history is not pinned — pin it from the CAT fee filings when backtest parity over
2024–2026 needs the cent).

## Charging model and the one open question

Alpaca's schedule states fees accrue intraday and are charged at end of day with the
total rounded up to $0.01. Its support page (https://alpaca.markets/support/regulatory-fees)
reads as if SEC and TAF are each rounded up per trade. The canonical model implements
**end-of-day, per-component round-up**. The reconciliation tolerance
`$0.01 × (3 + 2 × sell_fills)` admits the per-trade reading (at most one extra cent per
sell for SEC and one for TAF) so a live session can decide which reading is true; tighten
it to `$0.03` once observed sessions agree with one reading.

## Reconciliation

`GET /api/brokers/{broker}/fees/session-reconciliation?session_open_ms=<int>` (the
calendar's session open of a trading day, ET-anchored `int64 ms UTC`) prices every
*effective* SQLite fill dated that ET calendar day, sums the `FEE` activities Alpaca posted
for that date, and returns a verdict: `within_tolerance`, `drift`, `pending` (no `FEE`
posted yet, within 24 h after the day ends), `unobserved` (fills but no `FEE` after the
grace period, or a `FEE` row without `net_amount`), `no_fills`, `rate_unpinned`, or
`unavailable` (no active SQLite Clerk). Implementation:
`PythonDataService/app/services/alpaca_fee_reconciliation.py`. The activity read is
bounded (the broker port follows at most three newest-first pages of 100).

## Validation

- `PythonDataService/tests/broker/alpaca/test_regulatory_fees.py` — regime boundaries,
  sides, the TAF cap boundary (50,205 vs 50,206 shares), unpinned handling, settlement.
- Golden `FEE-001` (`PythonDataService/tests/fixtures/golden/broker-fees/FEE-001/v1/`),
  `reference_kind=hand_computed`, `atol=0, rtol=0`, asserted by
  `PythonDataService/tests/fixtures/test_alpaca_regulatory_fees_fixture.py`.
- No observed-fee fixture exists yet: the paper account has produced no `FEE` activity in
  any captured payload, and whether paper accounts emit them is unverified. The first live
  session (ADR 0059 slice 8) is the first observation.

## Follow-ups (not this slice)

- Engine wiring: `app/engine/execution/fill_model.py` exposes `compute_fee(quantity, fill_price)`
  without side or trade date; the IBKR tier model stays there until a fill-model
  signature carries both.
- Per-component observed reconciliation once `activity_sub_type` is mapped onto
  `BrokerActivity` (today only the day's total is compared).
- CAT rate history before 2026-09-01.
```

- [ ] **Step 2: Add the registry section and row**

In `docs/math-sources-of-truth.md`, insert this block right after the table that follows `### Broker display read models` (i.e. immediately before the line `### Indicators — Python-canonical, ported from LEAN`):

```markdown
### Broker fees (pass-through)

| Concept | Canonical | Legacy / duplicates | Reference | Validated against | Status |
|---|---|---|---|---|---|
| Alpaca equity regulatory fees (SEC §31, FINRA TAF, FINRA CAT; EOD per-component cent round-up) | `PythonDataService/app/broker/alpaca/regulatory_fees.py` | none (the IBKR tier model in `app/research/parity/ibkr_commission.py` is a different concept — broker commission, not regulatory pass-through) | Alpaca Broker Fee Schedule (retrieved 2026-09-07); SEC fee-rate advisories 2024-2/2025-2/2026-2; FINRA SR-FINRA-2024-019 — see [alpaca-regulatory-fees](references/alpaca-regulatory-fees.md) | `PythonDataService/tests/broker/alpaca/test_regulatory_fees.py`; golden `FEE-001` in `tests/fixtures/test_alpaca_regulatory_fees_fixture.py` (hand_computed, atol=0) | canonical — 4-field provenance block present |

```

- [ ] **Step 3: Verify the docs gates**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/research/documentation -q -k "sources_of_truth or math_sources or registry" ; echo "exit=$?"` — if no test is selected, run `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/research/documentation -q` and confirm it is green (the docs contract tests parse `docs/math-sources-of-truth.md`).

- [ ] **Step 4: Commit**

```bash
cd /Users/inkant/learn-ai && git add docs/references/alpaca-regulatory-fees.md docs/math-sources-of-truth.md && git commit -m "docs(references): Alpaca regulatory fee note with sourced rate table; registry row (ADR 0059 D6)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Reconciliation schema and pure function

**Files:**
- Create: `PythonDataService/app/schemas/alpaca_fee_reconciliation.py`
- Create: `PythonDataService/app/services/alpaca_fee_reconciliation.py` (pure part only; Task 5 appends the facade to this same file)
- Test: `PythonDataService/tests/services/test_alpaca_fee_reconciliation.py`

**Interfaces:**
- Consumes: Task 1 (`fees_for_fill`, `settle_session`, `RateNotPinnedError`); `app.broker.contract.models.BrokerActivity` (fields `broker`, `activity_id`, `activity_type`, `category`, `symbol`, `side`, `quantity`, `price`, `net_amount: float | None`, `occurred_at_ms: int | None`, `observed_at_ms: int`); `app.broker.alpaca.clerk.models.EpochMs`; `app.utils.session_anchors.et_date_at_ms`, `et_midnight_ms`.
- Produces:
  - `SessionFill(side: OrderSide, quantity: Decimal, fill_price: Decimal)` frozen dataclass
  - `reconcile_session_fees(*, broker: str, account_id: str, session_open_ms: int, fills: Sequence[SessionFill], fee_activities: Sequence[BrokerActivity], now_ms: int) -> SessionFeeReconciliation`
  - `FEE_POSTING_GRACE_MS = 86_400_000`
  - Schema `SessionFeeReconciliation` and `PredictedSessionFees`, `FeeReconciliationVerdict`

- [ ] **Step 1: Write the failing tests**

Create `PythonDataService/tests/services/test_alpaca_fee_reconciliation.py`:

```python
"""Predicted-vs-observed session fee reconciliation (ADR 0059 D6)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.broker.contract.models import BrokerActivity, OrderSide
from app.services.alpaca_fee_reconciliation import (
    FEE_POSTING_GRACE_MS,
    SessionFill,
    reconcile_session_fees,
)
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.utils.session_anchors import et_midnight_ms

D = Decimal
TRADE_DATE = date(2026, 9, 8)
SESSION_OPEN_MS = session_open_ms_utc(TRADE_DATE)
DAY_START_MS = et_midnight_ms(TRADE_DATE)
DAY_END_MS = et_midnight_ms(TRADE_DATE + timedelta(days=1))

# The six FEE-001 session fills: predicted sec 14.95 + taf 29.39 + cat 0.49 = 44.83
SESSION_FILLS = (
    SessionFill(side=OrderSide.SELL, quantity=D("100"), fill_price=D("250.00")),
    SessionFill(side=OrderSide.BUY, quantity=D("100"), fill_price=D("250.00")),
    SessionFill(side=OrderSide.SELL, quantity=D("60000"), fill_price=D("10.00")),
    SessionFill(side=OrderSide.SELL, quantity=D("50205"), fill_price=D("1.00")),
    SessionFill(side=OrderSide.SELL, quantity=D("50206"), fill_price=D("1.00")),
    SessionFill(side=OrderSide.BUY, quantity=D("0.5"), fill_price=D("400.00")),
)


def _fee(activity_id: str, net_amount: float | None, occurred_at_ms: int | None = DAY_START_MS) -> BrokerActivity:
    return BrokerActivity(
        broker="alpaca",
        activity_id=activity_id,
        activity_type="FEE",
        category="non_trade_activity",
        symbol=None,
        side=None,
        quantity=None,
        price=None,
        net_amount=net_amount,
        occurred_at_ms=occurred_at_ms,
        observed_at_ms=DAY_END_MS,
    )


def _reconcile(fills=SESSION_FILLS, activities=(), now_ms: int = DAY_END_MS + 1):
    return reconcile_session_fees(
        broker="alpaca",
        account_id="123456789",
        session_open_ms=SESSION_OPEN_MS,
        fills=fills,
        fee_activities=activities,
        now_ms=now_ms,
    )


def test_prediction_and_window_are_reported() -> None:
    result = _reconcile()

    assert result.predicted is not None
    assert (result.predicted.sec_usd, result.predicted.taf_usd, result.predicted.cat_usd) == (14.95, 29.39, 0.49)
    assert result.predicted.total_usd == 44.83
    assert (result.fill_count, result.sell_fill_count) == (6, 4)
    assert (result.fill_window_start_ms, result.fill_window_end_ms) == (DAY_START_MS, DAY_END_MS)
    assert result.tolerance_usd == 0.11  # 0.01 × (3 + 2 × 4)


def test_observed_within_tolerance() -> None:
    result = _reconcile(activities=(_fee("f1", -14.95), _fee("f2", -29.39), _fee("f3", -0.55)))

    assert result.verdict == "within_tolerance"
    assert result.observed_total_usd == 44.89
    assert result.delta_usd == 0.06
    assert result.observed_activity_count == 3


def test_observed_drift_beyond_tolerance() -> None:
    result = _reconcile(activities=(_fee("f1", -45.10),))

    assert result.verdict == "drift"
    assert result.delta_usd == 0.27


def test_fee_activities_from_other_days_are_ignored() -> None:
    result = _reconcile(activities=(_fee("yesterday", -44.83, DAY_START_MS - 1), _fee("no-date", -44.83, None)))

    assert result.observed_activity_count == 0
    assert result.verdict == "pending"  # nothing observed for this date; still inside the posting grace


def test_pending_inside_the_posting_grace_period() -> None:
    result = _reconcile(now_ms=DAY_END_MS + FEE_POSTING_GRACE_MS - 1)

    assert result.verdict == "pending"
    assert result.observed_total_usd is None


def test_unobserved_after_the_grace_period() -> None:
    result = _reconcile(now_ms=DAY_END_MS + FEE_POSTING_GRACE_MS)

    assert result.verdict == "unobserved"


def test_fee_row_without_net_amount_is_unobserved_not_zero() -> None:
    result = _reconcile(activities=(_fee("f1", -14.95), _fee("f2", None)))

    assert result.verdict == "unobserved"
    assert result.observed_total_usd is None
    assert result.observed_activity_count == 2


def test_no_fills_and_no_fees() -> None:
    result = _reconcile(fills=())

    assert result.verdict == "no_fills"
    assert result.predicted is not None
    assert result.predicted.total_usd == 0.0


def test_fees_observed_with_no_fills_is_drift() -> None:
    result = _reconcile(fills=(), activities=(_fee("f1", -1.00),))

    assert result.verdict == "drift"
    assert result.delta_usd == 1.0


def test_rate_unpinned_session_has_no_prediction() -> None:
    open_2025 = session_open_ms_utc(date(2025, 6, 2))

    result = reconcile_session_fees(
        broker="alpaca",
        account_id="123456789",
        session_open_ms=open_2025,
        fills=(SessionFill(side=OrderSide.SELL, quantity=D("1"), fill_price=D("1")),),
        fee_activities=(),
        now_ms=open_2025,
    )

    assert result.verdict == "rate_unpinned"
    assert result.predicted is None
    assert result.unpinned_components == ["cat"]
    assert result.tolerance_usd is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/services/test_alpaca_fee_reconciliation.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'app.services.alpaca_fee_reconciliation'`.

- [ ] **Step 3: Write the schema**

Create `PythonDataService/app/schemas/alpaca_fee_reconciliation.py`:

```python
"""Wire shape of one session's predicted-vs-observed fee reconciliation (ADR 0059 D6).

Money crosses this boundary as ``float`` USD (the model is ``Decimal`` inside);
every instant is ``int64 ms UTC``. ``session_open_ms`` is the trading date's
ET session-open anchor; the fill window is the ET calendar day around it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.models import EpochMs

FeeReconciliationVerdict = Literal[
    "within_tolerance",
    "drift",
    "pending",
    "unobserved",
    "no_fills",
    "rate_unpinned",
    "unavailable",
]


class PredictedSessionFees(BaseModel):
    """The model's end-of-day charge for the session, per component."""

    model_config = ConfigDict(frozen=True)

    sec_usd: float = Field(ge=0)
    taf_usd: float = Field(ge=0)
    cat_usd: float = Field(ge=0)
    total_usd: float = Field(ge=0)


class SessionFeeReconciliation(BaseModel):
    """Predicted fees for one ET trade date against the FEE activities Alpaca posted."""

    model_config = ConfigDict(frozen=True)

    broker: str
    account_id: str | None
    session_open_ms: EpochMs
    fill_window_start_ms: EpochMs
    fill_window_end_ms: EpochMs
    fill_count: int = Field(ge=0)
    sell_fill_count: int = Field(ge=0)
    predicted: PredictedSessionFees | None
    observed_total_usd: float | None
    observed_activity_count: int = Field(ge=0)
    delta_usd: float | None
    tolerance_usd: float | None
    verdict: FeeReconciliationVerdict
    why: str
    unpinned_components: list[str]
    observed_at_ms: EpochMs
```

- [ ] **Step 4: Write the pure reconciliation**

Create `PythonDataService/app/services/alpaca_fee_reconciliation.py`:

```python
"""Predicted-vs-observed session fee reconciliation (ADR 0059 D6).

Observed FEE activities are the truth; the canonical model predicts them. This
module prices one ET trade date's effective fills with
``app.broker.alpaca.regulatory_fees``, sums the FEE activities Alpaca posted for
that date, and reports the difference with a verdict. Alpaca posts fees at end
of day dated the trade date (ET midnight anchor), so the observation window is
the ET calendar day, not the RTH session — extended-hours fills bill the same day.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from app.broker.alpaca.regulatory_fees import (
    FillFees,
    RateNotPinnedError,
    SessionFees,
    fees_for_fill,
    settle_session,
)
from app.broker.contract.models import BrokerActivity, OrderSide
from app.schemas.alpaca_fee_reconciliation import (
    FeeReconciliationVerdict,
    PredictedSessionFees,
    SessionFeeReconciliation,
)
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms

# Alpaca charges at end of day; give the FEE activity a day to post before
# "no observation" becomes a finding rather than a wait.
FEE_POSTING_GRACE_MS = 24 * 60 * 60 * 1000
_ONE_DAY = timedelta(days=1)
_CENT = Decimal("0.01")
_ZERO = Decimal("0")


@dataclass(frozen=True)
class SessionFill:
    """The three facts about a fill the fee model prices."""

    side: OrderSide
    quantity: Decimal
    fill_price: Decimal


@dataclass(frozen=True)
class _Frame:
    """Everything a verdict shares, computed once per reconciliation."""

    broker: str
    account_id: str | None
    session_open_ms: int
    fill_window_start_ms: int
    fill_window_end_ms: int
    fill_count: int
    sell_fill_count: int
    observed_activity_count: int
    observed_at_ms: int

    def verdict(
        self,
        verdict: FeeReconciliationVerdict,
        why: str,
        *,
        predicted: PredictedSessionFees | None = None,
        observed_total_usd: float | None = None,
        delta_usd: float | None = None,
        tolerance_usd: float | None = None,
        unpinned_components: Sequence[str] = (),
    ) -> SessionFeeReconciliation:
        return SessionFeeReconciliation(
            broker=self.broker,
            account_id=self.account_id,
            session_open_ms=self.session_open_ms,
            fill_window_start_ms=self.fill_window_start_ms,
            fill_window_end_ms=self.fill_window_end_ms,
            fill_count=self.fill_count,
            sell_fill_count=self.sell_fill_count,
            predicted=predicted,
            observed_total_usd=observed_total_usd,
            observed_activity_count=self.observed_activity_count,
            delta_usd=delta_usd,
            tolerance_usd=tolerance_usd,
            verdict=verdict,
            why=why,
            unpinned_components=list(unpinned_components),
            observed_at_ms=self.observed_at_ms,
        )


def _frame(
    *,
    broker: str,
    account_id: str | None,
    session_open_ms: int,
    fills: Sequence[SessionFill],
    fee_rows: Sequence[BrokerActivity],
    now_ms: int,
) -> _Frame:
    trade_date = et_date_at_ms(session_open_ms)
    return _Frame(
        broker=broker,
        account_id=account_id,
        session_open_ms=session_open_ms,
        fill_window_start_ms=et_midnight_ms(trade_date),
        fill_window_end_ms=et_midnight_ms(trade_date + _ONE_DAY),
        fill_count=len(fills),
        sell_fill_count=sum(1 for fill in fills if fill.side == OrderSide.SELL),
        observed_activity_count=len(fee_rows),
        observed_at_ms=now_ms,
    )


def _predicted(settled: SessionFees) -> PredictedSessionFees:
    return PredictedSessionFees(
        sec_usd=float(settled.sec),
        taf_usd=float(settled.taf),
        cat_usd=float(settled.cat),
        total_usd=float(settled.total),
    )


def _observed_total(fee_rows: Sequence[BrokerActivity]) -> Decimal | None:
    """The day's charge as a positive amount, or ``None`` when it cannot be known."""
    if not fee_rows or any(row.net_amount is None for row in fee_rows):
        return None
    return -sum((Decimal(str(row.net_amount)) for row in fee_rows), _ZERO)


def reconcile_session_fees(
    *,
    broker: str,
    account_id: str,
    session_open_ms: int,
    fills: Sequence[SessionFill],
    fee_activities: Sequence[BrokerActivity],
    now_ms: int,
) -> SessionFeeReconciliation:
    """Compare the model's end-of-day charge with the FEE activities dated the trade date."""
    trade_date = et_date_at_ms(session_open_ms)
    fee_rows = [
        activity
        for activity in fee_activities
        if activity.activity_type == "FEE"
        and activity.occurred_at_ms is not None
        and et_date_at_ms(activity.occurred_at_ms) == trade_date
    ]
    frame = _frame(
        broker=broker,
        account_id=account_id,
        session_open_ms=session_open_ms,
        fills=fills,
        fee_rows=fee_rows,
        now_ms=now_ms,
    )
    priced: list[FillFees] = [
        fees_for_fill(
            trade_date=trade_date,
            side=fill.side,
            quantity=fill.quantity,
            fill_price=fill.fill_price,
        )
        for fill in fills
    ]
    observed = _observed_total(fee_rows)
    try:
        settled = settle_session(priced)
    except RateNotPinnedError as exc:
        return frame.verdict(
            "rate_unpinned",
            f"no pinned rate for {', '.join(exc.components)} on this trade date; the model cannot predict this session",
            observed_total_usd=None if observed is None else float(observed),
            unpinned_components=exc.components,
        )
    predicted = _predicted(settled)
    # 3 cents of end-of-day rounding plus, per sell, the extra cent each of SEC and
    # TAF could carry under the per-trade-rounding reading of Alpaca's support page.
    tolerance = _CENT * (3 + 2 * frame.sell_fill_count)
    if observed is None:
        if not fills and not fee_rows:
            return frame.verdict("no_fills", "no fills and no FEE activity on this trade date", predicted=predicted, tolerance_usd=float(tolerance))
        if fee_rows:
            return frame.verdict("unobserved", "a FEE activity for this trade date carries no net_amount; the observed charge is unknown", predicted=predicted, tolerance_usd=float(tolerance))
        if now_ms < frame.fill_window_end_ms + FEE_POSTING_GRACE_MS:
            return frame.verdict("pending", "Alpaca has not posted this trade date's FEE activity yet (charged at end of day)", predicted=predicted, tolerance_usd=float(tolerance))
        return frame.verdict("unobserved", "no FEE activity was posted for this trade date within a day of its end", predicted=predicted, tolerance_usd=float(tolerance))
    delta = observed - settled.total
    within = abs(delta) <= tolerance
    return frame.verdict(
        "within_tolerance" if within else "drift",
        "observed charge agrees with the model within tolerance" if within else "observed charge differs from the model by more than the tolerance",
        predicted=predicted,
        observed_total_usd=float(observed),
        delta_usd=float(delta),
        tolerance_usd=float(tolerance),
    )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/services/test_alpaca_fee_reconciliation.py -q`
Expected: 10 passed. If a float assertion such as `delta_usd == 0.06` fails on representation, the model is converting at the wrong place — `delta` must be computed in `Decimal` and converted once (`float(Decimal("0.06"))` is exactly `0.06`); do not loosen the assertion.

- [ ] **Step 6: Lint and commit**

Run ruff at project scope — expected clean.

```bash
cd /Users/inkant/learn-ai && git add PythonDataService/app/schemas/alpaca_fee_reconciliation.py PythonDataService/app/services/alpaca_fee_reconciliation.py PythonDataService/tests/services/test_alpaca_fee_reconciliation.py && git commit -m "feat(services): predicted-vs-observed session fee reconciliation verdict (ADR 0059 D6)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Fills pager, facade, endpoint, contracts

**Files:**
- Modify: `PythonDataService/app/services/alpaca_fee_reconciliation.py` (append the pager and facade; add imports)
- Modify: `PythonDataService/app/routers/brokers.py` (one endpoint after `list_activities`, plus imports)
- Modify: `PythonDataService/tests/services/test_alpaca_fee_reconciliation.py` (append pager and facade tests)
- Create: `PythonDataService/tests/routers/test_broker_fee_reconciliation.py`
- Regenerate: the OpenAPI snapshot under `contracts/` and the Frontend generated types.

**Interfaces:**
- Consumes: `app.broker.alpaca.clerk.active_authority.get_active_clerk_runtime() -> ActiveClerkRuntime | None` (attributes `authority_kind`, `clerk`); `app.broker.alpaca.clerk.sqlite.runtime.SqliteAlpacaClerkFacade` (attributes `repository`, `account_id`); `app.broker.alpaca.clerk.sqlite.economic_projection.SqliteEconomicProjectionReader.from_repository(repo)`, `.account_executions(*, cursor, limit, origin, bot, symbol, state) -> ExecutionPage`, `.close()`, `MAX_FILL_PAGE_LIMIT = 100`; `ExecutionPage(account_id, authority_generation, control_revision, executions: tuple[ExecutionRow, ...], next_cursor)`; `ExecutionRow` (fields `fill_id, execution_id, order_ref, strategy_instance_id, origin, state, event_kind, symbol, side: OrderSide, quantity: float, price: float, fee, fee_fidelity, filled_at_ms, recorded_at_ms`); `app.broker.contract.ports.BrokerReadPort.list_activities(*, after_ms, limit) -> list[BrokerActivity]` (the port keeps rows with `occurred_at_ms >= after_ms`, at most three newest-first pages); `app.routers.brokers._resolve_port(broker) -> BrokerReadPort`; `app.lean_sidecar.trading_calendar.is_trading_day`, `session_open_ms_utc`; `app.utils.timestamps.now_ms_utc`; `app.utils.session_anchors.MAX_TIMESTAMP_MS` (already imported in the router).
- Produces: `session_fills(pager, *, window_start_ms, window_end_ms) -> list[SessionFill]`; `async session_fee_reconciliation(*, broker, port, session_open_ms, now_ms=None) -> SessionFeeReconciliation`; `GET /api/brokers/{broker}/fees/session-reconciliation?session_open_ms=`.

- [ ] **Step 1: Append the failing pager and facade tests**

Append to `PythonDataService/tests/services/test_alpaca_fee_reconciliation.py` (add the new imports at the top of the file, keeping the existing ones):

```python
from app.broker.alpaca.clerk.active_authority import set_active_clerk_runtime
from app.broker.alpaca.clerk.sqlite.economic_projection_models import ExecutionPage, ExecutionRow
from app.services.alpaca_fee_reconciliation import session_fee_reconciliation, session_fills


def _row(fill_id: str, filled_at_ms: int, side: OrderSide, quantity: float, price: float) -> ExecutionRow:
    return ExecutionRow(
        fill_id=fill_id,
        execution_id=None,
        order_ref=f"order-{fill_id}",
        strategy_instance_id=None,
        origin="strategy",
        state="effective",
        event_kind="fill",
        symbol="AAPL",
        side=side,
        quantity=quantity,
        price=price,
        fee=None,
        fee_fidelity="not_reported",
        filled_at_ms=filled_at_ms,
        recorded_at_ms=filled_at_ms,
    )


def _page(rows: tuple[ExecutionRow, ...], next_cursor: str | None) -> ExecutionPage:
    return ExecutionPage(
        account_id="123456789",
        authority_generation=1,
        control_revision=1,
        executions=rows,
        next_cursor=next_cursor,
    )


class _Pager:
    """Newest-first pages keyed by cursor, recording every call."""

    def __init__(self, pages: dict[str | None, ExecutionPage]) -> None:
        self.pages = pages
        self.calls: list[tuple[str | None, int, str | None]] = []

    def account_executions(self, *, cursor: str | None, limit: int, state: str | None) -> ExecutionPage:
        self.calls.append((cursor, limit, state))
        return self.pages[cursor]


def test_session_fills_keeps_only_the_window_and_stops_paging_before_it() -> None:
    pager = _Pager(
        {
            None: _page(
                (
                    _row("after", DAY_END_MS, OrderSide.SELL, 1.0, 1.0),
                    _row("late", DAY_END_MS - 1, OrderSide.SELL, 100.0, 250.0),
                    _row("early", DAY_START_MS, OrderSide.BUY, 0.5, 400.0),
                ),
                "page-2",
            ),
            "page-2": _page((_row("before", DAY_START_MS - 1, OrderSide.SELL, 7.0, 7.0),), "page-3"),
            "page-3": _page((), None),
        }
    )

    fills = session_fills(pager, window_start_ms=DAY_START_MS, window_end_ms=DAY_END_MS)

    assert fills == [
        SessionFill(side=OrderSide.SELL, quantity=D("100"), fill_price=D("250")),
        SessionFill(side=OrderSide.BUY, quantity=D("0.5"), fill_price=D("400")),
    ]
    assert pager.calls == [(None, 100, "effective"), ("page-2", 100, "effective")]


def test_session_fills_stops_when_pages_run_out() -> None:
    pager = _Pager({None: _page((_row("only", SESSION_OPEN_MS, OrderSide.SELL, 2.0, 3.0),), None)})

    fills = session_fills(pager, window_start_ms=DAY_START_MS, window_end_ms=DAY_END_MS)

    assert fills == [SessionFill(side=OrderSide.SELL, quantity=D("2"), fill_price=D("3"))]


class _Port:
    def __init__(self) -> None:
        self.calls: list[tuple[int | None, int]] = []

    async def list_activities(self, *, after_ms: int | None = None, limit: int = 100) -> list[BrokerActivity]:
        self.calls.append((after_ms, limit))
        return []


async def test_facade_is_unavailable_without_an_active_sqlite_clerk() -> None:
    set_active_clerk_runtime(None)
    port = _Port()

    result = await session_fee_reconciliation(broker="alpaca", port=port, session_open_ms=SESSION_OPEN_MS, now_ms=DAY_END_MS)

    assert result.verdict == "unavailable"
    assert result.account_id is None
    assert result.predicted is None
    assert (result.fill_window_start_ms, result.fill_window_end_ms) == (DAY_START_MS, DAY_END_MS)
    assert port.calls == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/services/test_alpaca_fee_reconciliation.py -q`
Expected: `ImportError: cannot import name 'session_fee_reconciliation'`.

- [ ] **Step 3: Append the pager and facade**

Add these imports to `PythonDataService/app/services/alpaca_fee_reconciliation.py` (keep them sorted with the existing block; ruff enforces order):

```python
from typing import Protocol

from app.broker.alpaca.clerk.active_authority import get_active_clerk_runtime
from app.broker.alpaca.clerk.sqlite.economic_projection import (
    MAX_FILL_PAGE_LIMIT,
    SqliteEconomicProjectionReader,
)
from app.broker.alpaca.clerk.sqlite.economic_projection_models import ExecutionPage, ExecutionState
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.contract.ports import BrokerReadPort
from app.utils.timestamps import now_ms_utc
```

Append to the end of the module:

```python
class ExecutionPager(Protocol):
    """The one read this module needs from the SQLite economic projection."""

    def account_executions(
        self, *, cursor: str | None, limit: int, state: ExecutionState | None
    ) -> ExecutionPage: ...


def session_fills(
    pager: ExecutionPager,
    *,
    window_start_ms: int,
    window_end_ms: int,
) -> list[SessionFill]:
    """Effective fills with ``window_start_ms <= filled_at_ms < window_end_ms``.

    Pages are newest-first, so paging stops as soon as a page ends before the
    window; the read is bounded by the day's fill count, not the account's history.
    """
    fills: list[SessionFill] = []
    cursor: str | None = None
    while True:
        page = pager.account_executions(cursor=cursor, limit=MAX_FILL_PAGE_LIMIT, state="effective")
        for row in page.executions:
            if window_start_ms <= row.filled_at_ms < window_end_ms:
                fills.append(
                    SessionFill(
                        side=row.side,
                        quantity=Decimal(str(row.quantity)),
                        fill_price=Decimal(str(row.price)),
                    )
                )
        reached_before_window = bool(page.executions) and page.executions[-1].filled_at_ms < window_start_ms
        if page.next_cursor is None or reached_before_window:
            return fills
        cursor = page.next_cursor


def _active_sqlite_clerk() -> SqliteAlpacaClerkFacade | None:
    runtime = get_active_clerk_runtime()
    if runtime is None or runtime.authority_kind != "sqlite":
        return None
    clerk = runtime.clerk
    return clerk if isinstance(clerk, SqliteAlpacaClerkFacade) else None


async def session_fee_reconciliation(
    *,
    broker: str,
    port: BrokerReadPort,
    session_open_ms: int,
    now_ms: int | None = None,
) -> SessionFeeReconciliation:
    """Reconcile one trade date: SQLite fills priced by the model vs Alpaca's FEE rows."""
    observed_at_ms = now_ms_utc() if now_ms is None else now_ms
    clerk = _active_sqlite_clerk()
    if clerk is None:
        frame = _frame(
            broker=broker,
            account_id=None,
            session_open_ms=session_open_ms,
            fills=(),
            fee_rows=(),
            now_ms=observed_at_ms,
        )
        return frame.verdict(
            "unavailable",
            "no active SQLite Clerk authority; the session's fills cannot be read",
        )
    frame = _frame(
        broker=broker,
        account_id=clerk.account_id,
        session_open_ms=session_open_ms,
        fills=(),
        fee_rows=(),
        now_ms=observed_at_ms,
    )
    reader = SqliteEconomicProjectionReader.from_repository(clerk.repository)
    try:
        fills = session_fills(
            reader,
            window_start_ms=frame.fill_window_start_ms,
            window_end_ms=frame.fill_window_end_ms,
        )
    finally:
        reader.close()
    activities = await port.list_activities(after_ms=frame.fill_window_start_ms)
    return reconcile_session_fees(
        broker=broker,
        account_id=clerk.account_id,
        session_open_ms=session_open_ms,
        fills=fills,
        fee_activities=activities,
        now_ms=observed_at_ms,
    )
```

If `now_ms_utc` is not the name exported by `app/utils/timestamps.py`, use the name `app/broker/alpaca/adapter.py` imports from that module (it aliases it as `now_ms`).

- [ ] **Step 4: Run the service tests**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/services/test_alpaca_fee_reconciliation.py -q`
Expected: 13 passed.

- [ ] **Step 5: Write the failing router tests**

Create `PythonDataService/tests/routers/test_broker_fee_reconciliation.py`:

```python
"""GET /api/brokers/{broker}/fees/session-reconciliation."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.broker.alpaca.clerk.active_authority import set_active_clerk_runtime
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.routers import brokers as brokers_router
from app.utils.session_anchors import et_midnight_ms

SESSION_OPEN_MS = session_open_ms_utc(date(2026, 9, 8))
PATH = "/api/brokers/alpaca/fees/session-reconciliation"


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(brokers_router.router)
    return app


class _Port:
    async def list_activities(self, *, after_ms: int | None = None, limit: int = 100) -> list:
        return []


@pytest.mark.parametrize(
    "session_open_ms",
    [
        SESSION_OPEN_MS + 60_000,  # a minute after the open is not the session anchor
        et_midnight_ms(date(2026, 9, 8)),  # ET midnight is a different anchor
        session_open_ms_utc(date(2026, 9, 4)) + 3 * 24 * 60 * 60 * 1000,  # Labor Day 2026-09-07
    ],
)
async def test_rejects_a_value_that_is_not_a_trading_days_session_open(session_open_ms: int) -> None:
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.get(PATH, params={"session_open_ms": session_open_ms})

    assert response.status_code == 422
    assert "session open" in response.json()["detail"]


async def test_reports_unavailable_without_an_active_sqlite_clerk(monkeypatch: pytest.MonkeyPatch) -> None:
    set_active_clerk_runtime(None)
    monkeypatch.setattr(brokers_router, "_resolve_port", lambda broker: _Port())

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.get(PATH, params={"session_open_ms": SESSION_OPEN_MS})

    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "unavailable"
    assert body["session_open_ms"] == SESSION_OPEN_MS
    assert body["predicted"] is None
```

- [ ] **Step 6: Run them to verify they fail**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/routers/test_broker_fee_reconciliation.py -q`
Expected: the 422 cases fail with status 404 (no route yet).

- [ ] **Step 7: Add the endpoint**

In `PythonDataService/app/routers/brokers.py`, add these imports in their sorted positions:

```python
from app.lean_sidecar.trading_calendar import is_trading_day, session_open_ms_utc
from app.schemas.alpaca_fee_reconciliation import SessionFeeReconciliation
from app.services.alpaca_fee_reconciliation import session_fee_reconciliation
from app.utils.session_anchors import et_date_at_ms
```

(If `MAX_TIMESTAMP_MS` is imported from `app.utils.session_anchors` already, extend that import line instead of adding a second one.) Then add, directly after the `list_activities` endpoint:

```python
@router.get(
    "/{broker}/fees/session-reconciliation",
    response_model=SessionFeeReconciliation,
)
async def get_session_fee_reconciliation(
    broker: str,
    session_open_ms: int = Query(ge=0, le=MAX_TIMESTAMP_MS),
) -> SessionFeeReconciliation:
    """Predicted-vs-observed regulatory fees for one trade date (ADR 0059 D6)."""
    trade_date = et_date_at_ms(session_open_ms)
    if not is_trading_day(trade_date) or session_open_ms_utc(trade_date) != session_open_ms:
        raise HTTPException(
            status_code=422,
            detail="session_open_ms must be the calendar's session open (ET) of a trading day",
        )
    port = _resolve_port(broker)
    return await session_fee_reconciliation(broker=broker, port=port, session_open_ms=session_open_ms)
```

- [ ] **Step 8: Run the router tests**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/routers/test_broker_fee_reconciliation.py tests/routers/test_broker_capability.py tests/routers/test_broker_disabled.py -q`
Expected: all pass. If `is_trading_day` raises for a date outside the calendar's range, look at how `app/lean_sidecar/trading_calendar.py` bounds its range and pick test dates inside it; do not catch the exception in the router.

- [ ] **Step 9: Regenerate the contracts**

Run:
```bash
cd /Users/inkant/learn-ai/PythonDataService && .venv/bin/python scripts/export_openapi_contract.py && .venv/bin/python scripts/export_openapi_contract.py --check && cd /Users/inkant/learn-ai/Frontend && npm run codegen:openapi && npm run codegen:check
```
Expected: both checks exit 0 after regeneration. `git status` shows the OpenAPI snapshot under `contracts/` and the Frontend generated types changed; nothing else.

- [ ] **Step 10: Lint and commit**

Run ruff at project scope — expected clean. Then:

```bash
cd /Users/inkant/learn-ai && git add PythonDataService/app/services/alpaca_fee_reconciliation.py PythonDataService/app/routers/brokers.py PythonDataService/tests/services/test_alpaca_fee_reconciliation.py PythonDataService/tests/routers/test_broker_fee_reconciliation.py contracts/ Frontend/src/app/generated/ && git status --short && git commit -m "feat(api): GET /brokers/{broker}/fees/session-reconciliation prices SQLite fills against Alpaca FEE activities (ADR 0059 D6)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

If `git status --short` before the commit lists a generated file outside `contracts/` or `Frontend/src/app/generated/`, stage that exact path too and name it in the report.

---

### Task 6: Final gates on the branch tree

**Files:** none created. This task produces a report only.

- [ ] **Step 1: Project-scope lint**

Run: `/Users/inkant/learn-ai/PythonDataService/.venv/bin/ruff check /Users/inkant/learn-ai/PythonDataService/app/ /Users/inkant/learn-ai/PythonDataService/tests/`
Expected: no findings.

- [ ] **Step 2: Targeted tests for every touched surface and its consumers**

Run: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/test_regulatory_fees.py tests/fixtures tests/services/test_alpaca_fee_reconciliation.py tests/routers tests/research/documentation -q -p no:cacheprovider`
Expected: green. Anything red is yours to fix unless it is also red on `origin/master` at the same path — establish that baseline before calling it pre-existing, and name it in the report.

- [ ] **Step 3: Contract checks**

Run: `cd /Users/inkant/learn-ai/PythonDataService && .venv/bin/python scripts/export_openapi_contract.py --check && cd /Users/inkant/learn-ai/Frontend && npm run codegen:check`
Expected: both exit 0.

- [ ] **Step 4: Report**

State the exact commands, their exit codes, test counts, and any baseline-confirmed pre-existing failures. No commit.

---

## Rulings recorded while planning (carry into the SDD ledger)

- **Per-component pinning, not a single earliest date.** SEC and TAF are pinned back to 2024; CAT only from 2026-09-01. A single cut-off would make the model useless for any 2024–2026 backtest; a fabricated CAT history would violate "numerical claims require receipts". Cost if wrong: a `rate_unpinned` verdict on historical sessions until CAT history is pinned.
- **Buys never owe SEC/TAF, so those are `0` for buys even where the sell rate is unpinned.** The formula is identically zero; refusing would be false uncertainty.
- **Canonical settlement = end-of-day per-component ceiling** (the fee schedule, the primary source). The support page's per-trade reading is admitted through the tolerance, not a second policy. Cost if wrong: a `drift` on the first live session that the reconciliation itself will explain.
- **Fill window = the ET calendar day, not the RTH session.** Alpaca bills by trade date and slice 3 brings extended-hours fills; the endpoint's input stays the ADR's trading-date anchor (session open).
- **`SessionFill` is the reconciliation's input contract**, not a wrapper: three fields, constructed once by the pager, so the pure function and its tests do not carry the fifteen-field `ExecutionRow`.
- **Fixture trade dates are ET session-open anchors (`int64 ms UTC`)**, per temporal rigor; the generator imports the canonical calendar for them and is therefore run with `-m` (an empty `scripts/fixture_generators/__init__.py` is added).
- **The fixture test resolves its directory directly** rather than through `golden_support.registry`; the manifest test still validates the entry.
- **No engine wiring, no Frontend, no FEE-activity capture** in this slice: `FillModel.compute_fee` lacks side and date; nothing renders the verdict yet; capturing real `FEE` rows needs the owner's paper keys and consent.
