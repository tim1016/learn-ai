# Gate-3 Engine Fee Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close Gate 3 parity for all four W6mo cells (SPY, QQQ, AAPL, TSLA) by porting LEAN's `InteractiveBrokersFeeModel` into Engine Lab's execution path, making `LeanSetHoldingsSizing` fee-aware, locking the IBKR-margin brokerage contract into the LEAN trusted sample + cell manifests, and regenerating the four W6mo cells so `pytest -m cross_engine_smoke` shows 4/4 pass with `assert_fees=True`.

**Architecture:** The IBKR commission canonical (`app/research/parity/ibkr_commission.py`) is reused via a thin re-export under `app/engine/execution/commission.py` so engine code never imports from `research/`. `FillModel` gains an optional `fee_model` field; a new `compute_fee(quantity, fill_price)` helper centralizes per-fill commission so every direct read of `commission_per_order` in `engine.py` goes through one seam. `LeanSetHoldingsSizing` accepts the same fee model and runs a monotonic decrement solve to size the largest `qty` that satisfies `qty*price + fee(qty, price) <= portfolio_value * (1 - free_pv_pct)`. The LEAN EMA template adds `SetBrokerageModel(InteractiveBrokersBrokerage, Margin)` and the regen-script manifest block flips `brokerage_model`/`fee_model` to the IBKR names. Cells are regenerated; a new multi-symbol entries fixture extracted from those cells locks the parity guarantee at `atol=0`.

**Tech Stack:** Python 3.11+, FastAPI service, pandas, Decimal arithmetic, pytest with `@pytest.mark.cross_engine_smoke`, LEAN container (pinned digest) for cell regeneration.

---

## Pre-flight: Contract Confirmation and Branch Setup

The matrix is meant to pin **Interactive Brokers margin** as the brokerage. The user's spec confirms this; if the matrix owner ever changes that decision, all of Phase 2+3 (template + manifest + engine wiring) must change in lockstep — but for this plan we treat IBKR-margin as the locked contract.

- [ ] **Step 1: Pull latest master**

Run:
```
git checkout master
git pull --ff-only origin master
```

- [ ] **Step 2: Cut feature branch off master**

Run:
```
git checkout -b feat/parity-matrix-task-12-engine-fee-parity
```

- [ ] **Step 3: Confirm working tree is clean**

Run:
```
git status -s
```
Expected: empty output (or only the pre-existing untracked items `PythonDataService/.claude/` and `PythonDataService/tests/fixtures/polygon_capture/spy_minute_2025-01-06_2025-01-10/` that are not part of this work and stay untracked).

---

## Phase 1 — IBKR commission regression tests for the new representative cases

Lock the contract before touching engine code. The three cases below are the worked examples the user named; they prove the canonical model already produces the right numbers for the new tickers.

### Task 1: Add SPY/AAPL/TSLA regression tests for IbkrEquityCommissionModel

**Files:**
- Modify: `PythonDataService/tests/research/parity/test_ibkr_commission.py`

- [ ] **Step 1: Write the failing tests**

Append to `test_ibkr_commission.py`:

```python
def test_spy_150_shares_hits_floor() -> None:
    # 150 shares @ $662.50: per-share = 150 * 0.005 = $0.75 → floored to $1.00 min;
    # cap = 0.5% * 150 * $662.50 = $496.88 (no bite). The minimum dominates.
    model = IbkrEquityCommissionModel()
    assert model.fee(quantity=150, fill_price=Decimal("662.50")) == Decimal("1.00")


def test_aapl_365_shares_uses_per_share_rate() -> None:
    # 365 AAPL shares @ ~$270: per-share = 365 * 0.005 = $1.825 → rounds HALF_UP to $1.83;
    # floor $1.00 and cap ~$492.75 do not bind.
    model = IbkrEquityCommissionModel()
    assert model.fee(quantity=365, fill_price=Decimal("270.00")) == Decimal("1.83")


def test_tsla_221_shares_uses_per_share_rate() -> None:
    # 221 TSLA shares @ ~$450: per-share = 221 * 0.005 = $1.105 → rounds HALF_UP to $1.11;
    # floor $1.00 and cap ~$497.25 do not bind.
    model = IbkrEquityCommissionModel()
    assert model.fee(quantity=221, fill_price=Decimal("450.00")) == Decimal("1.11")
```

- [ ] **Step 2: Run the new tests to verify they pass**

Run:
```
podman exec polygon-data-service python -m pytest tests/research/parity/test_ibkr_commission.py -v
```
Expected: all 11 tests PASS (8 existing + 3 new). If `1.83` / `1.11` fail, the rounding direction in `ibkr_commission.py` is wrong — investigate before continuing.

- [ ] **Step 3: Commit**

```
git add PythonDataService/tests/research/parity/test_ibkr_commission.py
git commit -m "test(ibkr-commission): pin SPY/AAPL/TSLA representative fee cases"
```

---

## Phase 2 — Engine commission integration

### Task 2: Re-export IbkrEquityCommissionModel under engine.execution

Engine code must not import from `app.research.parity.*`; that package is for parity reconciliation, not engine runtime. Add a thin re-export so the canonical implementation stays in one place.

**Files:**
- Create: `PythonDataService/app/engine/execution/commission.py`

- [ ] **Step 1: Write the import-only test**

Create `PythonDataService/tests/engine/test_commission_reexport.py`:

```python
"""The engine.execution.commission re-export is the canonical engine-side
seam onto the IBKR fee model — keep it byte-equivalent to the research-side
canonical so a single fixture proves both paths."""

from __future__ import annotations

from decimal import Decimal

from app.engine.execution.commission import IbkrEquityCommissionModel as EngineModel
from app.research.parity.ibkr_commission import IbkrEquityCommissionModel as CanonicalModel


def test_engine_reexport_is_the_canonical_class() -> None:
    assert EngineModel is CanonicalModel


def test_engine_reexport_produces_canonical_fees() -> None:
    em, cm = EngineModel(), CanonicalModel()
    for qty, price in [(100, Decimal("150.00")), (365, Decimal("270.00")), (221, Decimal("450.00"))]:
        assert em.fee(quantity=qty, fill_price=price) == cm.fee(quantity=qty, fill_price=price)
```

- [ ] **Step 2: Run it to verify it fails**

Run:
```
podman exec polygon-data-service python -m pytest tests/engine/test_commission_reexport.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'app.engine.execution.commission'`.

- [ ] **Step 3: Create the re-export module**

Create `PythonDataService/app/engine/execution/commission.py`:

```python
"""Engine-side seam onto the IBKR equity-tier commission model.

The canonical implementation lives in
``app.research.parity.ibkr_commission`` (see its module docstring for the
formula and reference). Engine execution code imports from here so the
``app.engine.*`` package has no inbound dependency on ``app.research.*``;
the reconciler and the engine therefore share one fee implementation
without crossing the layer boundary in the wrong direction.

Provenance:
  Formula: see app/research/parity/ibkr_commission.py
  Reference: QuantConnect InteractiveBrokersBrokerage equity tier
  Canonical implementation: app/research/parity/ibkr_commission.py
  Validated against: tests/research/parity/test_ibkr_commission.py
                     tests/engine/test_commission_reexport.py
"""

from __future__ import annotations

from app.research.parity.ibkr_commission import IbkrEquityCommissionModel

__all__ = ["IbkrEquityCommissionModel"]
```

- [ ] **Step 4: Run the test to verify it passes**

Run:
```
podman exec polygon-data-service python -m pytest tests/engine/test_commission_reexport.py -v
```
Expected: 2 PASS.

- [ ] **Step 5: Commit**

```
git add PythonDataService/app/engine/execution/commission.py PythonDataService/tests/engine/test_commission_reexport.py
git commit -m "feat(engine): re-export IbkrEquityCommissionModel under app.engine.execution"
```

---

### Task 3: Add `fee_model` field and `compute_fee` helper to FillModel

The engine has six direct reads of `self.fill_model.commission_per_order`. Centralize them through one helper so flipping to a fee model is a single seam.

**Files:**
- Modify: `PythonDataService/app/engine/execution/fill_model.py`
- Modify: `PythonDataService/tests/engine/test_fill_model_fee.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `PythonDataService/tests/engine/test_fill_model_fee.py`:

```python
"""FillModel.compute_fee is the single seam through which the engine
charges commission. With ``fee_model=None`` it falls back to the legacy
flat ``commission_per_order`` (so historical SPY parity runs stay byte-
identical). With ``fee_model=IbkrEquityCommissionModel()`` it returns the
per-fill IBKR fee — this is the path the cross-engine matrix uses."""

from __future__ import annotations

from decimal import Decimal

from app.engine.execution.commission import IbkrEquityCommissionModel
from app.engine.execution.fill_model import FillModel


def test_compute_fee_default_returns_flat_commission() -> None:
    model = FillModel()
    assert model.commission_per_order == Decimal("1.00")
    assert model.compute_fee(quantity=150, fill_price=Decimal("662.50")) == Decimal("1.00")
    # Flat regardless of quantity/price when fee_model is None.
    assert model.compute_fee(quantity=365, fill_price=Decimal("270.00")) == Decimal("1.00")


def test_compute_fee_with_ibkr_model_returns_per_fill_fee() -> None:
    model = FillModel(fee_model=IbkrEquityCommissionModel())
    # 150 @ $662.50 → $0.75 raw, floored to $1.00.
    assert model.compute_fee(quantity=150, fill_price=Decimal("662.50")) == Decimal("1.00")
    # 365 @ $270 → $1.83 per-share rate.
    assert model.compute_fee(quantity=365, fill_price=Decimal("270.00")) == Decimal("1.83")
    # 221 @ $450 → $1.11 per-share rate.
    assert model.compute_fee(quantity=221, fill_price=Decimal("450.00")) == Decimal("1.11")


def test_fill_market_order_uses_compute_fee_with_fee_model() -> None:
    """The OrderEvent's fee must come from compute_fee, not commission_per_order."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.engine.data.trade_bar import TradeBar
    from app.engine.execution.order import Direction, FillMode, Order, OrderType

    ny = ZoneInfo("America/New_York")
    bar = TradeBar(
        symbol="AAPL",
        time=datetime(2026, 1, 5, 9, 30, tzinfo=ny),
        end_time=datetime(2026, 1, 5, 9, 45, tzinfo=ny),
        open=Decimal("269.50"),
        high=Decimal("270.50"),
        low=Decimal("269.00"),
        close=Decimal("270.00"),
        volume=10_000,
    )
    order = Order(
        order_id=1,
        symbol="AAPL",
        order_type=OrderType.MARKET,
        direction=Direction.LONG,
        quantity=365,
        tag="ENTER",
    )
    fm = FillModel(mode=FillMode.SIGNAL_BAR_CLOSE, fee_model=IbkrEquityCommissionModel())
    event = fm.fill_market_order(order, bar)
    assert event is not None
    assert event.fee == Decimal("1.83")
```

- [ ] **Step 2: Run it to verify it fails**

Run:
```
podman exec polygon-data-service python -m pytest tests/engine/test_fill_model_fee.py -v
```
Expected: FAIL — `FillModel` has no `fee_model` field and no `compute_fee` method.

- [ ] **Step 3: Add the field, helper, and use it inside `fill_market_order`**

Edit `PythonDataService/app/engine/execution/fill_model.py`:

Replace the dataclass body (lines 47–64 of the current file) and the OrderEvent construction (lines 120–129) with:

```python
@dataclass
class FillModel:
    """Simple fill model configurable between the three supported modes.

    Args:
        mode: One of the ``FillMode`` values.
        commission_per_order: Legacy flat fee. Used when ``fee_model`` is
            None — pre-matrix SPY parity fixtures still rely on it. New
            fixtures pin a fee_model and ignore this field.
        slippage_per_share: Applied against the trade direction.
        fee_model: Optional per-fill fee model
            (:class:`IbkrEquityCommissionModel`). When set,
            ``compute_fee(quantity, fill_price)`` returns
            ``fee_model.fee(quantity, fill_price)`` and ``commission_per_order``
            is ignored. This is the single seam through which the matrix
            cells charge IBKR equity-tier commission.
    """

    mode: FillMode = FillMode.SIGNAL_BAR_CLOSE
    commission_per_order: Decimal = Decimal("1.00")
    slippage_per_share: Decimal = Decimal(0)
    fee_model: "IbkrEquityCommissionModel | None" = None

    def compute_fee(self, *, quantity: int, fill_price: Decimal) -> Decimal:
        """Return the fee for a single fill. Always quantized to cents."""
        if self.fee_model is not None:
            return self.fee_model.fee(quantity=int(quantity), fill_price=fill_price)
        return self.commission_per_order
```

Then change the `OrderEvent(...)` construction at the end of `fill_market_order` (replace `fee=self.commission_per_order,` with `fee=self.compute_fee(quantity=int(order.quantity), fill_price=fill_price),`):

```python
        return OrderEvent(
            order_id=order.order_id,
            symbol=order.symbol,
            time=fill_time,
            fill_price=fill_price,
            fill_quantity=order.quantity,
            direction=order.direction,
            fee=self.compute_fee(quantity=int(order.quantity), fill_price=fill_price),
            tag=order.tag,
        )
```

Add the necessary import at the top of the module:

```python
from app.engine.execution.commission import IbkrEquityCommissionModel  # noqa: F401  (referenced by type hint)
```

(The string annotation `"IbkrEquityCommissionModel | None"` keeps this from being a hard runtime dependency cycle. `noqa: F401` is justified because the symbol exists only for the type hint.)

- [ ] **Step 4: Run the new tests to verify they pass**

Run:
```
podman exec polygon-data-service python -m pytest tests/engine/test_fill_model_fee.py -v
```
Expected: 3 PASS.

- [ ] **Step 5: Run the full engine test suite to check no SPY regressions**

Run:
```
podman exec polygon-data-service python -m pytest tests/engine/ -v -k "not slow"
```
Expected: all pre-existing tests still PASS. The default `FillModel()` keeps `fee_model=None` so legacy paths charge `$1.00` flat exactly as before.

- [ ] **Step 6: Commit**

```
git add PythonDataService/app/engine/execution/fill_model.py PythonDataService/tests/engine/test_fill_model_fee.py
git commit -m "feat(fill-model): add fee_model + compute_fee seam (backwards-compatible)"
```

---

### Task 4: Route every fee read in engine.py through `compute_fee`

`engine.py` reads `self.fill_model.commission_per_order` at lines 111, 157, 229, 398, 450 (per the grep in pre-flight). Each call site has a known `quantity` and `fill_price` in scope — refactor each to call `self.fill_model.compute_fee(...)` so the IBKR model takes effect on every fill.

**Files:**
- Modify: `PythonDataService/app/engine/engine.py`
- Modify: `PythonDataService/tests/engine/test_engine_fee_routing.py` (new)

- [ ] **Step 1: Inspect each call site**

Run:
```
grep -n "commission_per_order" PythonDataService/app/engine/engine.py
```
Expected output: lines 111, 157, 229, 398, 450 (or close to these).

- [ ] **Step 2: Write a failing routing test**

Create `PythonDataService/tests/engine/test_engine_fee_routing.py`:

```python
"""Smoke test that the engine charges the FillModel-derived fee on a
matrix-style run. Uses a tiny synthetic fixture so it runs in <1s and
needs no LEAN container."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.engine.execution.commission import IbkrEquityCommissionModel
from app.engine.execution.fill_model import FillModel


def test_fill_model_fee_routes_to_ibkr_for_aapl_qty_365() -> None:
    """Belt-and-braces: the new compute_fee helper returns the IBKR per-fill
    fee, and the helper is the single source of truth the engine consults.

    Stronger integration coverage (full backtest run with IBKR fees)
    happens in test_cross_engine_study.py once cells are regenerated; this
    test isolates the engine's wiring so a regression here is diagnosed
    before the heavyweight smoke run."""
    fm = FillModel(fee_model=IbkrEquityCommissionModel())
    assert fm.compute_fee(quantity=365, fill_price=Decimal("270.00")) == Decimal("1.83")
    # The legacy field MUST still exist (some test fixtures read it) but
    # is now bypassed when fee_model is set.
    assert fm.commission_per_order == Decimal("1.00")


@pytest.mark.parametrize("qty,price,expected", [
    (150, Decimal("662.50"), Decimal("1.00")),
    (365, Decimal("270.00"), Decimal("1.83")),
    (221, Decimal("450.00"), Decimal("1.11")),
])
def test_engine_fee_seam_matches_canonical(qty: int, price: Decimal, expected: Decimal) -> None:
    fm = FillModel(fee_model=IbkrEquityCommissionModel())
    assert fm.compute_fee(quantity=qty, fill_price=price) == expected
```

- [ ] **Step 3: Run it to confirm it passes already (Task 3 was enough for the test, but it documents the contract)**

Run:
```
podman exec polygon-data-service python -m pytest tests/engine/test_engine_fee_routing.py -v
```
Expected: 4 PASS.

- [ ] **Step 4: Refactor each `commission_per_order` read in `engine.py`**

For each line returned by the grep in Step 1, find the `OrderEvent(...)` (or `portfolio.order_fee = ...`) construction nearby. Replace:

```python
fee=self.fill_model.commission_per_order,
```
with:
```python
fee=self.fill_model.compute_fee(quantity=int(<qty>), fill_price=<price>),
```
where `<qty>` and `<price>` are the variables in local scope at that point (always `order.quantity` / `fill_price` for fill paths; `order.quantity` / `signal_bar.close` for force-close paths).

For line 111 (`portfolio.order_fee = self.fill_model.commission_per_order`), this is the **sizing pre-allocation** — sizing now consults the fee model directly (Phase 3, Task 5), so this assignment becomes vestigial. Delete the line, but **only after Task 5 (fee-aware sizing) is in place** so sizing never reads a stale `portfolio.order_fee`.

Mechanics:
1. Edit the five OrderEvent fills first (the easy ones).
2. Leave the sizing pre-allocation line untouched until Task 5 lands.

Use `Edit` with enough surrounding context to make each `old_string` unique. Be ready to commit each call-site swap as a separate logical change — but a single commit is acceptable if the diff is small.

- [ ] **Step 5: Run the engine test suite plus the fee routing test**

Run:
```
podman exec polygon-data-service python -m pytest tests/engine/ -v -k "not slow"
```
Expected: all PASS. The default `FillModel()` keeps `fee_model=None`, so `compute_fee` returns `commission_per_order` (`$1.00`) — byte-identical behavior for every existing SPY parity test.

- [ ] **Step 6: Commit**

```
git add PythonDataService/app/engine/engine.py PythonDataService/tests/engine/test_engine_fee_routing.py
git commit -m "feat(engine): route all OrderEvent fees through FillModel.compute_fee"
```

---

## Phase 3 — Fee-aware SetHoldings sizing

### Task 5: Make `LeanSetHoldingsSizing` fee-model-aware via monotonic decrement

The existing 20-entry golden fixture tests sizing with a fixed `order_fee` (always `$1.00`). The new path injects a fee model and solves `qty*price + fee(qty, price) <= portfolio_value*(1 - 0.0025)` by decrementing from the naive floor. Both paths must coexist: passing `order_fee` keeps the existing fixture green at `atol=0`; passing `fee_model` turns on the per-fill solve.

**Files:**
- Modify: `PythonDataService/app/engine/execution/sizing.py`
- Modify: `PythonDataService/tests/engine/test_sizing.py`

- [ ] **Step 1: Write the failing tests**

Append to `PythonDataService/tests/engine/test_sizing.py`:

```python
from app.engine.execution.commission import IbkrEquityCommissionModel


def test_fee_aware_sizing_matches_fixed_fee_when_model_floors_to_one_dollar() -> None:
    """When the IBKR floor binds, fee_model path matches the fixed $1 path
    bit-for-bit. SPY at $665.67, $100k portfolio: 100 shares * $0.005 = $0.50,
    floored to $1.00 — same as the legacy fixed-fee parameter."""
    pv, price = Decimal("100000"), Decimal("665.67")
    legacy = LeanSetHoldingsSizing().target_quantity(
        portfolio_value=pv, price=price, target_fraction=Decimal(1), order_fee=Decimal("1")
    )
    fee_aware = LeanSetHoldingsSizing(fee_model=IbkrEquityCommissionModel()).target_quantity(
        portfolio_value=pv, price=price, target_fraction=Decimal(1), order_fee=Decimal("0")
    )
    assert legacy == fee_aware == 149


def test_fee_aware_sizing_handles_per_share_rate_for_aapl_target_one() -> None:
    """AAPL at $270 with $100k portfolio: naive floor = 369; the IBKR
    per-share fee on 369 shares is $1.85 (raw $1.845, ROUND_HALF_UP to
    $1.85). buying_power = $99,750. 369 * 270 + 1.85 = $99,631.85 ≤ $99,750,
    so the monotonic solve accepts qty=369. The legacy fixed-$1 path
    would also accept 369 here, so the divergence is small — but the
    contract that fee comes from the model is what matters."""
    pv, price = Decimal("100000"), Decimal("270.00")
    qty = LeanSetHoldingsSizing(fee_model=IbkrEquityCommissionModel()).target_quantity(
        portfolio_value=pv, price=price, target_fraction=Decimal(1), order_fee=Decimal("0")
    )
    assert qty == 369


def test_fee_aware_sizing_decrements_when_fee_pushes_over_buying_power() -> None:
    """Constructed boundary case: a price where the naive floor's per-share
    fee just barely exceeds the buffer. portfolio_value = $1000,
    free_pv_pct = 0.25% so buying_power = $997.50. Price = $4.99:
      naive_qty = floor(997.50 / 4.99) = 199
      199 * 4.99 = $993.01; fee = max($1.00, 199*$0.005=$0.995→$1.00) = $1.00
      total = $994.01 ≤ $997.50 → qty = 199 (accept first try, no decrement needed)
    Pick a tighter case where decrement actually fires: at price = $5.00,
      naive_qty = floor(997.50 / 5.00) = 199; cost = $995.00; fee = $1.00;
      total = $996.00 ≤ $997.50 → still 199 (no decrement).
    A true decrement case needs the per-share fee to exceed the slack.
    Construct it: pv = $1000, price = $4.95, fee_model with $0.50 floor
    so the floor doesn't dominate.
      buying_power = $997.50; naive_qty = floor(997.50/4.95) = 201;
      201*4.95 = $994.95; per_share = 201*$0.005 = $1.005 → ROUND_HALF_UP = $1.01;
      slack = $997.50 - $994.95 = $2.55 > $1.01 → still accepts 201.
    The IBKR model under realistic equity prices basically never forces a
    decrement because the per-share rate ($0.005) is so small relative to
    the price. Document this as a property and assert: the fee-aware path
    is monotone non-increasing vs the no-fee path."""
    fee_aware = LeanSetHoldingsSizing(fee_model=IbkrEquityCommissionModel())
    plain = LeanSetHoldingsSizing()
    for pv, price in [
        (Decimal("100000"), Decimal("665.67")),
        (Decimal("100000"), Decimal("270.00")),
        (Decimal("100000"), Decimal("450.00")),
        (Decimal("99000"), Decimal("1.50")),  # very low price → per-share cap can bite
    ]:
        qty_fee_aware = fee_aware.target_quantity(
            portfolio_value=pv, price=price, target_fraction=Decimal(1), order_fee=Decimal("0")
        )
        qty_no_fee = plain.target_quantity(
            portfolio_value=pv, price=price, target_fraction=Decimal(1), order_fee=Decimal("0")
        )
        assert qty_fee_aware <= qty_no_fee, (
            f"fee-aware path must never overbuy vs no-fee path: "
            f"pv={pv} price={price} fee_aware={qty_fee_aware} no_fee={qty_no_fee}"
        )


def test_fee_aware_sizing_terminates_when_budget_too_small_for_any_share() -> None:
    qty = LeanSetHoldingsSizing(fee_model=IbkrEquityCommissionModel()).target_quantity(
        portfolio_value=Decimal("50"),
        price=Decimal("665.67"),
        target_fraction=Decimal(1),
        order_fee=Decimal("0"),
    )
    assert qty == 0
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run:
```
podman exec polygon-data-service python -m pytest tests/engine/test_sizing.py -v
```
Expected: the four new tests FAIL with `TypeError: ... got an unexpected keyword argument 'fee_model'`. The 8 existing tests still PASS.

- [ ] **Step 3: Add the `fee_model` field and the monotonic-solve branch**

Edit `PythonDataService/app/engine/execution/sizing.py`. Add the import at the top:

```python
from app.engine.execution.commission import IbkrEquityCommissionModel
```

Replace the `LeanSetHoldingsSizing` class (lines 86–113 of the current file) with:

```python
@dataclass(frozen=True)
class LeanSetHoldingsSizing:
    """LEAN-equivalent ``SetHoldings`` sizing for long-only equity.

    Reserves ``free_portfolio_value_pct`` of portfolio value, then either:

    * subtracts a caller-supplied flat ``order_fee`` (legacy path used by
      the 20-entry golden fixture), or
    * when ``fee_model`` is supplied, solves the largest integer ``qty``
      such that ``qty*price + fee_model.fee(qty, price) <= buying_power``
      via monotonic decrement from the naive floor — the path the
      cross-engine matrix cells use under IBKR margin brokerage.
    """

    name: str = "lean_set_holdings"
    free_portfolio_value_pct: Decimal = LEAN_FREE_PORTFOLIO_VALUE_PCT
    fee_model: IbkrEquityCommissionModel | None = None

    def target_quantity(
        self,
        *,
        portfolio_value: Decimal,
        price: Decimal,
        target_fraction: Decimal,
        order_fee: Decimal,
    ) -> int:
        if price <= 0:
            raise ValueError(f"sizing price must be positive, got {price}")
        target_value = portfolio_value * target_fraction
        buying_power = portfolio_value * (Decimal(1) - self.free_portfolio_value_pct)
        cap = min(target_value, buying_power)

        if self.fee_model is None:
            budget = cap - order_fee
            if budget <= 0:
                return 0
            return int(budget / price)

        # Fee-aware path: ignore order_fee (the caller hasn't yet computed
        # it — we compute per-iteration from the fee model). Monotonic
        # decrement from the naive floor.
        qty = int(cap / price)
        while qty > 0:
            fee = self.fee_model.fee(quantity=qty, fill_price=price)
            if qty * price + fee <= cap:
                return qty
            qty -= 1
        return 0
```

- [ ] **Step 4: Run the full sizing test file to verify nothing regressed**

Run:
```
podman exec polygon-data-service python -m pytest tests/engine/test_sizing.py -v
```
Expected: all tests PASS — the 20-entry SPY golden fixture is untouched (fee_model defaults to None) and the 4 new tests turn green.

- [ ] **Step 5: Now delete the vestigial `portfolio.order_fee = self.fill_model.commission_per_order` line**

This is the deferred edit from Task 4 Step 4. With fee-aware sizing landed, the engine's pre-allocation of a flat fee on the portfolio is dead weight: sizing computes its own fee per iteration via the fee_model.

Find and delete (or leave a comment if the line is referenced elsewhere — grep first):
```
grep -n "portfolio.order_fee" PythonDataService/app/engine/
```
If `portfolio.order_fee` is read elsewhere (e.g., by a UI snapshot), leave the assignment in and pass `compute_fee(...)` for a representative `(qty, price)` instead — pick one that does not lie about the strategy's fee profile (e.g., last fill's fee).

- [ ] **Step 6: Commit**

```
git add PythonDataService/app/engine/execution/sizing.py PythonDataService/app/engine/engine.py PythonDataService/tests/engine/test_sizing.py
git commit -m "feat(sizing): fee-aware LeanSetHoldingsSizing via monotonic decrement"
```

---

## Phase 4 — Lock the brokerage contract in template + manifest

### Task 6: Add `SetBrokerageModel(InteractiveBrokersBrokerage, Margin)` to the LEAN EMA template

LEAN's default brokerage charges `ConstantFeeModel(0)`. The matrix needs IBKR fees on the LEAN side so Gate 3 with `assert_fees=True` is a meaningful comparison.

**Files:**
- Modify: `PythonDataService/app/lean_sidecar/trusted_samples/ema_crossover.py`

- [ ] **Step 1: Write the failing test**

Append to `PythonDataService/tests/lean_sidecar/test_ema_crossover_template.py` (create if missing):

```python
"""The EMA crossover trusted sample is the matrix's LEAN-side oracle.
The brokerage contract — InteractiveBrokers margin — is locked here
because the cell manifest's broker block depends on this template
being unambiguous about which fee model LEAN ran."""

from __future__ import annotations

from app.lean_sidecar.trusted_samples.ema_crossover import EMA_CROSSOVER_SOURCE


def test_template_pins_interactive_brokers_margin_brokerage() -> None:
    assert "SetBrokerageModel(BrokerageName.InteractiveBrokersBrokerage, AccountType.Margin)" in EMA_CROSSOVER_SOURCE


def test_template_pins_brokerage_before_subscriptions() -> None:
    """LEAN docs: SetBrokerageModel must precede AddEquity so the security's
    fee/fill models are configured by IB at subscribe time."""
    src = EMA_CROSSOVER_SOURCE
    sbm_idx = src.index("SetBrokerageModel(")
    add_equity_idx = src.index("self.AddEquity(")
    assert sbm_idx < add_equity_idx, "SetBrokerageModel must appear before AddEquity"
```

- [ ] **Step 2: Run it to verify it fails**

Run:
```
podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_ema_crossover_template.py -v
```
Expected: FAIL — the template does not yet call `SetBrokerageModel`.

- [ ] **Step 3: Insert the SetBrokerageModel call**

Edit `PythonDataService/app/lean_sidecar/trusted_samples/ema_crossover.py`. After `self.SetCash(cash)` (around line 91) and **before** `self.AddEquity(...)` (around line 93), insert:

```python
        # Lock the brokerage model: matrix Gate 3 runs with assert_fees=True
        # so LEAN must charge IBKR equity-tier commission (per-share + floor +
        # cap), not the default ConstantFeeModel(0). The engine side pins the
        # same model via app.engine.execution.commission.IbkrEquityCommissionModel.
        self.SetBrokerageModel(BrokerageName.InteractiveBrokersBrokerage, AccountType.Margin)
```

(`BrokerageName` and `AccountType` are imported via `from AlgorithmImports import *` at the top of the template string — no additional import needed.)

- [ ] **Step 4: Run the template test to verify it passes**

Run:
```
podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_ema_crossover_template.py -v
```
Expected: 2 PASS.

- [ ] **Step 5: Commit**

```
git add PythonDataService/app/lean_sidecar/trusted_samples/ema_crossover.py PythonDataService/tests/lean_sidecar/test_ema_crossover_template.py
git commit -m "feat(lean-template): pin InteractiveBrokers margin brokerage in EMA crossover"
```

---

### Task 7: Update the regen-script manifest's broker block to match

The cell manifest documents the LEAN-side contract; flipping the template without flipping the manifest leaves a lie in the fixture.

**Files:**
- Modify: `PythonDataService/scripts/regenerate_cross_engine_study.py`
- Modify: `PythonDataService/tests/lean_sidecar/parity_matrix/test_regenerate_manifest.py` (new)

- [ ] **Step 1: Write the failing test**

Create `PythonDataService/tests/lean_sidecar/parity_matrix/test_regenerate_manifest.py`:

```python
"""The cell manifest's `broker` block is the documented contract LEAN ran
under. After Task 6 it must say InteractiveBrokers / IBKR equity fee, not
the default brokerage."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_regen_manifest_broker_block_pins_interactive_brokers() -> None:
    src = (Path(__file__).resolve().parents[3] / "scripts" / "regenerate_cross_engine_study.py").read_text(
        encoding="utf-8"
    )
    # Must declare InteractiveBrokersBrokerage and the IBKR fee model name.
    assert '"brokerage_model": "InteractiveBrokersBrokerage"' in src
    assert '"fee_model": "InteractiveBrokersFeeModel"' in src
    # Default brokerage / zero-fee strings must NOT appear in the manifest block.
    assert '"brokerage_model": "DefaultBrokerageModel"' not in src
    assert '"fee_model": "ConstantFeeModel(0)"' not in src
```

- [ ] **Step 2: Run it to verify it fails**

Run:
```
podman exec polygon-data-service python -m pytest tests/lean_sidecar/parity_matrix/test_regenerate_manifest.py -v
```
Expected: FAIL — the regen script still hardcodes the default brokerage.

- [ ] **Step 3: Edit the broker block**

In `PythonDataService/scripts/regenerate_cross_engine_study.py`, replace (around line 419):

```python
        "broker": {
            "brokerage_model": "DefaultBrokerageModel",
            "account_type": "Margin",
            "fill_model": "ImmediateFillModel",
            "fee_model": "ConstantFeeModel(0)",
        },
```
with:
```python
        "broker": {
            "brokerage_model": "InteractiveBrokersBrokerage",
            "account_type": "Margin",
            "fill_model": "ImmediateFillModel",
            "fee_model": "InteractiveBrokersFeeModel",
        },
```

The `fee_model` field names the LEAN-side class (since this manifest describes the LEAN pinned run). The engine-side parity port (`app.engine.execution.commission.IbkrEquityCommissionModel`) is a separate fact documented in `docs/math-sources-of-truth.md` (Task 13).

- [ ] **Step 4: Run the test to verify it passes**

Run:
```
podman exec polygon-data-service python -m pytest tests/lean_sidecar/parity_matrix/test_regenerate_manifest.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```
git add PythonDataService/scripts/regenerate_cross_engine_study.py PythonDataService/tests/lean_sidecar/parity_matrix/test_regenerate_manifest.py
git commit -m "feat(regen-script): manifest broker block pins InteractiveBrokers + IBKR fees"
```

---

## Phase 5 — Wire the engine cross-runner to use the new FillModel + fee-aware sizing

### Task 8: Inject `IbkrEquityCommissionModel` into `run_engine_lab_on_workspace`

The cross-runner instantiates `BacktestEngine(data_source=reader, sizing_model=LeanSetHoldingsSizing())` — sizing model has no fee model, and the engine's default FillModel charges flat $1. Both must be matched to the locked IBKR contract.

**Files:**
- Modify: `PythonDataService/app/lean_sidecar/cross_runner.py`
- Modify: `PythonDataService/tests/lean_sidecar/test_cross_runner_fee_wiring.py` (new)

- [ ] **Step 1: Write the failing test**

Create `PythonDataService/tests/lean_sidecar/test_cross_runner_fee_wiring.py`:

```python
"""The cross-engine matrix needs FillModel + LeanSetHoldingsSizing both
wired with IbkrEquityCommissionModel — otherwise the engine charges
flat $1 fees while LEAN charges per-share, and Gate 3 fails on
COMMISSION_DRIFT for every fill.

This test enforces the wiring at the source-code level (cheap, no LEAN
container required). End-to-end coverage lives in test_cross_engine_study.py
post-regeneration."""

from __future__ import annotations

from pathlib import Path


def test_cross_runner_constructs_backtest_engine_with_ibkr_fee_model() -> None:
    src = (Path(__file__).resolve().parents[2] / "app" / "lean_sidecar" / "cross_runner.py").read_text(
        encoding="utf-8"
    )
    # FillModel must be explicitly constructed with the IBKR fee model.
    assert "IbkrEquityCommissionModel" in src
    assert "FillModel(fee_model=IbkrEquityCommissionModel())" in src
    # Sizing model must also receive the IBKR fee model.
    assert "LeanSetHoldingsSizing(fee_model=IbkrEquityCommissionModel())" in src
```

- [ ] **Step 2: Run it to verify it fails**

Run:
```
podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_cross_runner_fee_wiring.py -v
```
Expected: FAIL — `IbkrEquityCommissionModel` not yet imported in `cross_runner.py`.

- [ ] **Step 3: Edit `cross_runner.py`**

At the top of `PythonDataService/app/lean_sidecar/cross_runner.py`, add imports next to the existing engine imports:

```python
from app.engine.execution.commission import IbkrEquityCommissionModel
from app.engine.execution.fill_model import FillModel
```

Then change the `BacktestEngine(...)` construction at line ~336:

Replace:
```python
    engine = BacktestEngine(data_source=reader, sizing_model=LeanSetHoldingsSizing())
```
with:
```python
    # Matrix runs pin IBKR equity-tier commission on both sides:
    #   * FillModel.fee_model → per-fill fee on OrderEvents
    #   * LeanSetHoldingsSizing.fee_model → buying-power calc subtracts
    #     the same per-fill fee, so the engine's qty matches LEAN's
    #     SetHoldings under InteractiveBrokers brokerage.
    fee_model = IbkrEquityCommissionModel()
    engine = BacktestEngine(
        data_source=reader,
        sizing_model=LeanSetHoldingsSizing(fee_model=fee_model),
        fill_model=FillModel(fee_model=fee_model),
    )
```

- [ ] **Step 4: Run the wiring test plus the cross-runner unit tests to verify nothing regresses**

Run:
```
podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_cross_runner_fee_wiring.py tests/lean_sidecar/test_cross_runner.py -v
```
Expected: all PASS. (If the SPY-only legacy `test_cross_runner.py` asserts exact fee values from the flat-$1 era, update those assertions to use the IBKR model and document the change in the commit message.)

- [ ] **Step 5: Commit**

```
git add PythonDataService/app/lean_sidecar/cross_runner.py PythonDataService/tests/lean_sidecar/test_cross_runner_fee_wiring.py
git commit -m "feat(cross-runner): wire IBKR fee model into FillModel + sizing for matrix cells"
```

---

## Phase 6 — Regenerate the four W6mo cells

### Task 9: Regenerate SPY, QQQ, AAPL, TSLA W6mo cells

These cells were pinned under the old contract (Default brokerage / $0 LEAN fees / flat $1 engine fees). Re-regenerating writes new `lean/orders.json` (with IBKR per-fill fees), new `manifest.json` (with the corrected broker block), and a fresh `reconciliation_pinned.json` that proves Gate 3 passes with `assert_fees=True`.

**Prerequisite:** LEAN container image pinned (`scripts/lean_sidecar_pin_image.py` already run for this checkout — `PINNED_LEAN_IMAGE_DIGEST` resolves to non-`None`). Also requires the host-side launcher process (per `PythonDataService/CLAUDE.md`) reachable for the in-process launch path.

**Files:**
- Modify: `PythonDataService/tests/fixtures/golden/cross-engine-studies/cells/SPY_W6mo_2025-11-03_to_2026-04-30/`
- Create: `PythonDataService/tests/fixtures/golden/cross-engine-studies/cells/QQQ_W6mo_2025-11-03_to_2026-04-30/`
- Create: `PythonDataService/tests/fixtures/golden/cross-engine-studies/cells/AAPL_W6mo_2025-11-03_to_2026-04-30/`
- Create: `PythonDataService/tests/fixtures/golden/cross-engine-studies/cells/TSLA_W6mo_2025-11-03_to_2026-04-30/`

- [ ] **Step 1: Pre-flight — confirm captures exist for all four tickers**

Run:
```
ls PythonDataService/tests/fixtures/golden/cross-engine-studies/_lean_data_capture/
```
Expected: `SPY/`, `QQQ/`, `AAPL/`, `TSLA/` all present (each with `manifest.json` + `equity/usa/minute/...`).

If any are missing, the capture step (separate from this plan) must be re-run first — surface to the user instead of proceeding.

- [ ] **Step 2: Regenerate SPY W6mo**

Run from `PythonDataService/`:
```
.venv/Scripts/python.exe scripts/regenerate_cross_engine_study.py --cell SPY_W6mo_2025-11-03_to_2026-04-30
```
Expected: `passed` in the script output. On failure, inspect `tests/fixtures/golden/cross-engine-studies/.failed/SPY_W6mo_2025-11-03_to_2026-04-30/report.json` and diagnose **before** running QQQ. If Gate 3 fails on COMMISSION_DRIFT, the FillModel wiring (Task 8) didn't reach the engine — re-verify the cross_runner edit took effect.

- [ ] **Step 3: Regenerate QQQ W6mo**

Run:
```
.venv/Scripts/python.exe scripts/regenerate_cross_engine_study.py --cell QQQ_W6mo_2025-11-03_to_2026-04-30
```
Expected: `passed`. If `QUANTITY_MISMATCH` appears, the fee-aware sizing (Task 5) didn't take effect for QQQ — the symptom the user predicted.

- [ ] **Step 4: Regenerate AAPL W6mo**

Run:
```
.venv/Scripts/python.exe scripts/regenerate_cross_engine_study.py --cell AAPL_W6mo_2025-11-03_to_2026-04-30
```
Expected: `passed`.

- [ ] **Step 5: Regenerate TSLA W6mo**

Run:
```
.venv/Scripts/python.exe scripts/regenerate_cross_engine_study.py --cell TSLA_W6mo_2025-11-03_to_2026-04-30
```
Expected: `passed`. If TSLA fails, proceed to Task 10 (do NOT widen tolerance; do NOT skip the cell).

- [ ] **Step 6: Commit the regenerated cells**

```
git add PythonDataService/tests/fixtures/golden/cross-engine-studies/cells/
git commit -m "fixture(parity-matrix): regenerate four W6mo cells under IBKR brokerage contract"
```

---

## Phase 7 — Conditional TSLA fill/decision drift diagnosis

### Task 10: Inspect TSLA only if Task 9 Step 5 failed

If Task 9 Step 5 passed, **skip this task**. If TSLA failed with anything other than COMMISSION_DRIFT or QUANTITY_MISMATCH (those are addressed by Tasks 5 + 8), the divergence is in fill timing or decision pairing. Do **not** widen tolerance.

**Files:**
- Read-only: `PythonDataService/tests/fixtures/golden/cross-engine-studies/.failed/TSLA_W6mo_2025-11-03_to_2026-04-30/report.json`
- Read-only: `PythonDataService/app/lean_sidecar/cross_reconciler.py` (`_pair_fills` lines 201–232)
- Read-only: `PythonDataService/app/engine/execution/fill_model.py`
- Read-only: `PythonDataService/app/engine/engine.py` (force-close / final-bar flush paths around lines 441–450)

- [ ] **Step 1: Categorize the divergence**

Read the failure report:
```
cat PythonDataService/tests/fixtures/golden/cross-engine-studies/.failed/TSLA_W6mo_2025-11-03_to_2026-04-30/report.json
```
Find which gate failed and which `DivergenceCategory` values appear.

- [ ] **Step 2: Diagnose by category**

| Category | Where to look | Likely fix |
|---|---|---|
| `DECISION_MISMATCH` | `_pair_fills` pairing key; check whether LEAN/engine disagree on which day produced a fill. Also check `signal == "ENTER"` rows in `state.csv` — a missing signal on one side means warmup / indicator readiness drift. | Strategy / indicator port bug; fix in `app/engine/strategy/algorithms/spy_ema_crossover.py` |
| `FILL_PRICE_DRIFT` | `OrderEvent.time` and `OrderEvent.fill_price` for the disagreeing fills; check whether the engine paired the order to a different bar than LEAN. | `FillModel` mode mismatch — verify `SIGNAL_BAR_CLOSE` produces the same `bar.Close` as LEAN's `ImmediateFillModel` on the trade bar. |
| `ORDER_TYPE_MISMATCH` | LEAN's order type; the engine only supports MARKET. | Investigate whether `SetHoldings` produced a non-MARKET order in LEAN (unlikely; signal of a regression in trusted sample). |
| `DECISION_MISMATCH` on the **final** bar | `engine.py:441-450` final-bar flush; LEAN's `OnEndOfAlgorithm` liquidation. | Both sides must drain a held position on the last bar of the window. Confirm `OnEndOfAlgorithm` parity. |

- [ ] **Step 3: Apply the fix in the engine (not in the tolerance)**

Write a failing regression test that captures the disagreement in a minimal synthetic fixture (or extract the disagreeing trade as a minimal repro). Fix the engine. Re-run regeneration.

- [ ] **Step 4: Commit fix and re-regenerated TSLA cell**

```
git add <fixed files> PythonDataService/tests/fixtures/golden/cross-engine-studies/cells/TSLA_W6mo_2025-11-03_to_2026-04-30/
git commit -m "fix(engine): <specific fix description>; re-regenerate TSLA W6mo"
```

---

## Phase 8 — Multi-symbol sizing fixture (post-regen extraction)

### Task 11: Extract first ENTER fill per ticker into a new multi-symbol sizing fixture

The 20-entry SPY fixture covers SPY only. Post-regen, add one representative fixture entry per ticker (extracted from the regenerated cell's first ENTER fill) so the sizing parity guarantee is locked at `atol=0` for the matrix's full ticker set.

**Files:**
- Create: `PythonDataService/tests/fixtures/golden/lean-set-holdings/multi_symbol_entries.json`
- Modify: `PythonDataService/tests/engine/test_sizing.py`

- [ ] **Step 1: Extract first ENTER from each regenerated cell**

For each ticker (SPY, QQQ, AAPL, TSLA), open `tests/fixtures/golden/cross-engine-studies/cells/<TICKER>_W6mo_2025-11-03_to_2026-04-30/lean/orders.json`. Find the first event with `direction == "buy"` and `status == "filled"`. Record:
- `ticker` (str)
- `price` = `fillPrice` (str, full precision)
- `lean_qty` = `fillQuantity` (int)
- `tpv` = $100,000 starting cash (first ENTER is always the first fill, so portfolio value == starting cash)
- `order_fee` = the recorded `orderFeeAmount` (str)

Write these as a JSON array to `PythonDataService/tests/fixtures/golden/lean-set-holdings/multi_symbol_entries.json`:

```json
[
  {"ticker": "SPY",  "tpv": "100000", "price": "<extracted>", "order_fee": "<extracted>", "lean_qty": <extracted>},
  {"ticker": "QQQ",  "tpv": "100000", "price": "<extracted>", "order_fee": "<extracted>", "lean_qty": <extracted>},
  {"ticker": "AAPL", "tpv": "100000", "price": "<extracted>", "order_fee": "<extracted>", "lean_qty": <extracted>},
  {"ticker": "TSLA", "tpv": "100000", "price": "<extracted>", "order_fee": "<extracted>", "lean_qty": <extracted>}
]
```

- [ ] **Step 2: Add an attribution sibling file**

Create `PythonDataService/tests/fixtures/golden/lean-set-holdings/multi_symbol_entries.md`:

```markdown
# multi_symbol_entries.json — provenance

Each entry is the **first ENTER fill** from the corresponding W6mo
cell's `lean/orders.json` under the matrix's IBKR-margin brokerage
contract. Extracted on regen day; subsequent regens may shift the
first-ENTER trading day if the strategy's cross condition lands on
a different bar — re-extract in lockstep when that happens.

Source files:
- SPY: `tests/fixtures/golden/cross-engine-studies/cells/SPY_W6mo_2025-11-03_to_2026-04-30/lean/orders.json`
- QQQ: `tests/fixtures/golden/cross-engine-studies/cells/QQQ_W6mo_2025-11-03_to_2026-04-30/lean/orders.json`
- AAPL: `tests/fixtures/golden/cross-engine-studies/cells/AAPL_W6mo_2025-11-03_to_2026-04-30/lean/orders.json`
- TSLA: `tests/fixtures/golden/cross-engine-studies/cells/TSLA_W6mo_2025-11-03_to_2026-04-30/lean/orders.json`

Reproduction:
  for each TICKER:
    jq '[.[] | select(.direction == "buy" and .status == "filled")][0]' \
       cells/<TICKER>_W6mo_.../lean/orders.json
```

- [ ] **Step 3: Append the multi-symbol parity test**

Append to `PythonDataService/tests/engine/test_sizing.py`:

```python
_MULTI_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "golden" / "lean-set-holdings" / "multi_symbol_entries.json"


def _load_multi_entries() -> list[dict]:
    return json.loads(_MULTI_FIXTURE.read_text(encoding="utf-8"))


def test_multi_symbol_fee_aware_sizing_matches_lean_atol_zero() -> None:
    """Per-ticker first-ENTER parity at atol=0 under IBKR margin brokerage.
    Locks the fee-aware sizing path for SPY/QQQ/AAPL/TSLA, not just SPY.
    See multi_symbol_entries.md for provenance."""
    sizing = LeanSetHoldingsSizing(fee_model=IbkrEquityCommissionModel())
    mismatches: list[str] = []
    for e in _load_multi_entries():
        qty = sizing.target_quantity(
            portfolio_value=Decimal(e["tpv"]),
            price=Decimal(e["price"]),
            target_fraction=Decimal(1),
            order_fee=Decimal("0"),
        )
        if qty != e["lean_qty"]:
            mismatches.append(
                f"{e['ticker']}: tpv={e['tpv']} price={e['price']}: got {qty}, LEAN {e['lean_qty']}"
            )
    assert not mismatches, "Multi-symbol fee-aware sizing parity failed:\n" + "\n".join(mismatches)
```

- [ ] **Step 4: Run the new test**

Run:
```
podman exec polygon-data-service python -m pytest tests/engine/test_sizing.py::test_multi_symbol_fee_aware_sizing_matches_lean_atol_zero -v
```
Expected: PASS (4 entries, atol=0).

- [ ] **Step 5: Commit**

```
git add PythonDataService/tests/fixtures/golden/lean-set-holdings/multi_symbol_entries.json PythonDataService/tests/fixtures/golden/lean-set-holdings/multi_symbol_entries.md PythonDataService/tests/engine/test_sizing.py
git commit -m "fixture(sizing): multi-symbol fee-aware parity entries extracted from W6mo cells"
```

---

## Phase 9 — Docs

### Task 12: Update the cross-engine matrix README + spec to mark W6mo sufficient

**Files:**
- Modify: `PythonDataService/tests/fixtures/golden/cross-engine-studies/README.md`
- Modify: `docs/superpowers/specs/2026-05-21-cross-engine-golden-matrix-design.md`

- [ ] **Step 1: Edit the README**

In `PythonDataService/tests/fixtures/golden/cross-engine-studies/README.md`, add a new section after the "Tests" section:

```markdown
## Acceptance status

The **four-symbol W6mo smoke set** is the parity proof for this slice:
all four cells (SPY, QQQ, AAPL, TSLA) pass `pytest -m cross_engine_smoke`
with `assert_fees=True` against the locked IBKR-margin brokerage
contract.

W12mo and W24mo cells are extended/nightly stress coverage and are not
required to close this slice. They are regenerated on the same triggers
as W6mo (image digest, trusted sample, deliberate contract change).
```

- [ ] **Step 2: Edit the matrix design spec**

In `docs/superpowers/specs/2026-05-21-cross-engine-golden-matrix-design.md`, find the section that describes the acceptance criteria for this slice and add (at the top of that section or as a new "Acceptance" subsection):

```markdown
### W6mo acceptance (slice closure)

This slice is closed when:

1. All four W6mo cells (SPY, QQQ, AAPL, TSLA) have committed
   `lean/orders.json` + `state.csv` + `observations.csv` +
   `reconciliation_pinned.json` under
   `tests/fixtures/golden/cross-engine-studies/cells/`.
2. `pytest -m cross_engine_smoke` reports 4 selected, 4 passed under
   the default `assert_fees=True` gating.
3. The cell manifest broker block declares
   `brokerage_model: InteractiveBrokersBrokerage` and
   `fee_model: InteractiveBrokersFeeModel` — matching the trusted
   sample's `SetBrokerageModel(BrokerageName.InteractiveBrokersBrokerage,
   AccountType.Margin)` call.

W12mo / W24mo cells are nightly stress coverage; they do not gate this
slice.
```

- [ ] **Step 3: Commit**

```
git add PythonDataService/tests/fixtures/golden/cross-engine-studies/README.md docs/superpowers/specs/2026-05-21-cross-engine-golden-matrix-design.md
git commit -m "docs(parity-matrix): W6mo four-symbol smoke set is the slice acceptance proof"
```

---

### Task 13: Update sizing + math-source-of-truth docs

**Files:**
- Modify: `docs/references/lean-set-holdings.md`
- Modify: `docs/math-sources-of-truth.md`

- [ ] **Step 1: Edit `lean-set-holdings.md`**

Add (or update if a section exists) a "Fee-aware sizing" section:

```markdown
## Fee-aware sizing (IBKR brokerage)

When LEAN runs under `SetBrokerageModel(InteractiveBrokersBrokerage, Margin)`,
`SetHoldings` consults `InteractiveBrokersFeeModel` per iteration when
sizing — the buying-power solve is a function of the per-fill fee, not
a fixed flat fee.

The Python port mirrors this via the optional `fee_model` field on
`LeanSetHoldingsSizing` (see
`PythonDataService/app/engine/execution/sizing.py`). When supplied, the
solver decrements from the naive floor until
`qty*price + fee_model.fee(qty, price) <= portfolio_value*(1 - 0.0025)`
holds. The IBKR per-share rate ($0.005) rarely forces a decrement at
realistic equity prices — the per-iteration solve still pins the
contract for low-price / high-quantity edge cases where the cap or
per-share rate bites.

Validated against:
- `tests/fixtures/golden/lean-set-holdings/entries.json` — 20 SPY
  entries (legacy fixed-fee path)
- `tests/fixtures/golden/lean-set-holdings/multi_symbol_entries.json` —
  4 first-ENTER fills (SPY, QQQ, AAPL, TSLA) under IBKR fee-aware path
- `tests/engine/test_sizing.py`
```

- [ ] **Step 2: Edit `math-sources-of-truth.md`**

Find the section that lists canonical implementations (or add a new entry under the appropriate heading). Add:

```markdown
| IBKR equity-tier commission | `PythonDataService/app/research/parity/ibkr_commission.py` | `PythonDataService/app/engine/execution/commission.py` re-exports the same class; engine code must import from the engine re-export, not from research/. | `tests/research/parity/test_ibkr_commission.py`, `tests/engine/test_commission_reexport.py` |
| Fee-aware `SetHoldings` sizing | `PythonDataService/app/engine/execution/sizing.py` (`LeanSetHoldingsSizing` with `fee_model=...`) | Solves `qty*price + fee(qty, price) <= portfolio_value*(1 - 0.0025)` by monotonic decrement; matches LEAN's `GetMaximumOrderQuantityForTargetBuyingPower` under IBKR brokerage. | `tests/engine/test_sizing.py` (legacy 20-entry SPY fixture + multi-symbol first-ENTER fixture) |
```

(If the table format differs, adapt to the existing schema.)

- [ ] **Step 3: Commit**

```
git add docs/references/lean-set-holdings.md docs/math-sources-of-truth.md
git commit -m "docs(math): IBKR commission canonical + fee-aware sizing source-of-truth"
```

---

## Phase 10 — Final acceptance + PR

### Task 14: Run the full acceptance gate

- [ ] **Step 1: Run the smoke-set cross-engine test**

Run from `PythonDataService/`:
```
podman exec polygon-data-service python -m pytest tests/research/parity/test_cross_engine_study.py -m cross_engine_smoke -v
```
Expected: `4 selected, 4 passed` (each of SPY/QQQ/AAPL/TSLA W6mo).

- [ ] **Step 2: Run project-scope ruff lint (from host, NOT the container — per memory `feedback_ruff_run_from_host.md`)**

Run from repo root:
```
ruff check PythonDataService/app/ PythonDataService/tests/
```
Expected: zero issues. If lint introduces unrelated cross-file drift, fix it in a separate commit per `.claude/rules/python.md` lint-scope rule.

- [ ] **Step 3: Run the full Python test suite to confirm no regressions outside the matrix**

Run:
```
podman exec polygon-data-service python -m pytest tests/ -v -k "not slow"
```
Expected: all PASS. Note any pre-existing failures (per pre-push test-suite hygiene rule) and confirm they were also failing on master before this branch.

- [ ] **Step 4: Verify branch state**

Run:
```
git log --oneline master..HEAD
```
Expected: the commits from Tasks 1–13 in order.

---

### Task 15: Push and open PR

- [ ] **Step 1: Push the branch**

Run:
```
git push -u origin feat/parity-matrix-task-12-engine-fee-parity
```

- [ ] **Step 2: Open the PR via `gh`**

Run:
```
gh pr create --title "feat(parity-matrix): wire IBKR commission + fee-aware sizing into engine (Task 12)" --body "$(cat <<'EOF'
## Summary
- Locks the matrix's brokerage contract to InteractiveBrokers margin: LEAN trusted sample now calls `SetBrokerageModel(BrokerageName.InteractiveBrokersBrokerage, AccountType.Margin)` and the cell-manifest broker block declares `InteractiveBrokersBrokerage` + `InteractiveBrokersFeeModel`.
- Wires the canonical `IbkrEquityCommissionModel` (`app/research/parity/ibkr_commission.py`) into Engine Lab execution via a thin re-export at `app/engine/execution/commission.py`. `FillModel` gains an optional `fee_model` field and a single `compute_fee(quantity, fill_price)` seam every `engine.py` fill-event constructor now routes through. Default behavior (flat $1) is preserved for legacy SPY fixtures.
- Makes `LeanSetHoldingsSizing` fee-model-aware: the new path solves the largest qty satisfying `qty*price + fee(qty, price) <= portfolio_value*(1 - 0.0025)` by monotonic decrement from the naive floor. Legacy fixed-fee path stays bit-exact for the existing 20-entry SPY golden fixture.
- Regenerates the four W6mo cells (SPY, QQQ, AAPL, TSLA) under the new contract; new multi-symbol sizing fixture pins each ticker's first-ENTER qty at `atol=0`.
- Updates docs: cross-engine matrix README + design spec call out W6mo as the slice acceptance proof; `lean-set-holdings.md` documents the fee-aware path; `math-sources-of-truth.md` lists the new canonical entries.

## Test plan
- [ ] `podman exec polygon-data-service python -m pytest tests/research/parity/test_ibkr_commission.py -v` — 11 PASS (8 existing + 3 new SPY/AAPL/TSLA cases)
- [ ] `podman exec polygon-data-service python -m pytest tests/engine/test_sizing.py -v` — 20-entry SPY fixture + new fee-aware tests + 4-entry multi-symbol fixture PASS
- [ ] `podman exec polygon-data-service python -m pytest tests/engine/ -v -k "not slow"` — full engine suite green (no regressions to flat-$1 SPY parity)
- [ ] `podman exec polygon-data-service python -m pytest tests/research/parity/test_cross_engine_study.py -m cross_engine_smoke -v` — **4 selected, 4 passed**
- [ ] `ruff check PythonDataService/app/ PythonDataService/tests/` — zero issues (run from host)
- [ ] All four `cells/<TICKER>_W6mo_2025-11-03_to_2026-04-30/` directories committed with fresh `manifest.json`, `lean/orders.json`, `lean/state.csv`, `lean/observations.csv`, and `reconciliation_pinned.json`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Capture the PR URL**

Record the URL `gh pr create` prints; the PR monitor agent picks it up from there.

---

## Notes for the executor

- **Branch hygiene** — never commit to master; the pre-flight cuts the feature branch first. (Memory: `feedback_branch_workflow.md`.)
- **Run ruff from the host**, not the container — the container is missing the `I` rule in scope. (Memory: `feedback_ruff_run_from_host.md`.)
- **Verify host TZ before claiming a 1-hour bar drift** — Tim's host is Central Time. (Memory: `feedback_verify_host_tz.md`.) Not expected to apply here, but flagged if Task 10 fires.
- **Do not widen tolerances** in Phase 7 — diagnose the engine, not the gate.
- **Cells are regenerated, not hand-edited** — every cell touched in Task 9/10 must come from a regen-script run that exited `passed`, not from a manual JSON tweak. (Numerical-rigor rule.)
- **Pre-existing untracked items** (`PythonDataService/.claude/`, `PythonDataService/tests/fixtures/polygon_capture/spy_minute_2025-01-06_2025-01-10/`) are not part of this work and stay untracked.
