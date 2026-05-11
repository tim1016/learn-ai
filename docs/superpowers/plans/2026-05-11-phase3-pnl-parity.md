# Phase 3 QC Trade-Level P&L Parity — Implementation Plan

> **For agentic workers:** Steps use checkbox (`- [ ]`) syntax. TDD where applicable. Frequent commits.

**Goal:** Build infrastructure to validate trade-level parity between our backtest engine and QC's recorded backtest for the AAPL single-symbol degenerate of the precomputed-ML-predictions tutorial.

**Architecture:** Add `FixtureDataReader` to feed QC's price CSV through the existing engine; add a standalone `IbkrEquityCommissionModel` callable used by the reconciler (not the engine); add a `QcReconciler` module that diffs QC's `qc_orders.json` against our trade log with a classified divergence taxonomy. Pytest harness ships with two skipped tests that activate when the fixture lands.

**Tech Stack:** Python 3.11+, pandas, Pydantic v2, pytest. Reuses `app/engine/execution/*` (FillMode.NEXT_BAR_OPEN already exists), `app/research/runs/runner.py`, and `app/research/ml/generators/quantconnect_fixture.py`.

**Spec:** `docs/superpowers/specs/2026-05-11-phase3-pnl-parity-design.md`

---

## File Structure

**Create:**
- `PythonDataService/app/research/parity/__init__.py`
- `PythonDataService/app/research/parity/fixture_data_reader.py`
- `PythonDataService/app/research/parity/ibkr_commission.py`
- `PythonDataService/app/research/parity/qc_reconciler.py`
- `PythonDataService/tests/research/parity/__init__.py`
- `PythonDataService/tests/research/parity/conftest.py`
- `PythonDataService/tests/research/parity/test_fixture_data_reader.py`
- `PythonDataService/tests/research/parity/test_ibkr_commission.py`
- `PythonDataService/tests/research/parity/test_qc_reconciler.py`
- `PythonDataService/tests/research/parity/test_qc_fixture_smoke.py` (skipped on master)
- `PythonDataService/tests/research/parity/test_qc_aapl_phase3_trade_parity.py` (skipped on master)
- `docs/references/reconciliations/.gitkeep`
- `PythonDataService/artifacts/.gitkeep` (the dir is tracked, contents are gitignored)

**Modify:**
- `.gitignore` (add `PythonDataService/artifacts/reconciliations/`)
- `.claude/rules/numerical-rigor.md` (append trade-level reconciliation taxonomy section)

---

## Task 1: Scaffolding & gitignore

- [ ] **Step 1.1** Create empty package files

  Write:
  - `PythonDataService/app/research/parity/__init__.py` — `"""QC parity & reconciliation tooling (Phase 3+)."""`
  - `PythonDataService/tests/research/parity/__init__.py` — empty

- [ ] **Step 1.2** Add `.gitignore` entry

  Append to repo root `.gitignore`:
  ```
  # Phase 3+ reconciliation runtime artifacts
  PythonDataService/artifacts/reconciliations/
  ```

- [ ] **Step 1.3** Create kept-empty artifact dir and reconciliation docs dir

  Write `PythonDataService/artifacts/.gitkeep` (empty file).
  Write `docs/references/reconciliations/.gitkeep` (empty file).

- [ ] **Step 1.4** Commit

  ```
  git add PythonDataService/app/research/parity/__init__.py \
          PythonDataService/tests/research/parity/__init__.py \
          PythonDataService/artifacts/.gitkeep \
          docs/references/reconciliations/.gitkeep \
          .gitignore
  git commit -m "chore(parity): scaffold app/research/parity + reconciliation artifact dirs"
  ```

---

## Task 2: `FixtureDataReader` (CSV → bars)

**Files:**
- Create: `PythonDataService/app/research/parity/fixture_data_reader.py`
- Test: `PythonDataService/tests/research/parity/test_fixture_data_reader.py`

- [ ] **Step 2.1** Examine the existing data-reader protocol

  Read `PythonDataService/app/research/runs/runner.py` to understand what callable shape `data_source_factory` expects and what type of bar object it yields.

- [ ] **Step 2.2** Write failing test

  `tests/research/parity/test_fixture_data_reader.py`:
  ```python
  from __future__ import annotations

  from datetime import date
  from decimal import Decimal
  from pathlib import Path

  import pytest

  from app.research.parity.fixture_data_reader import FixtureDataReader

  CSV_CONTENT = (
      "time,open,high,low,close,volume\n"
      "2026-02-10,189.50,190.10,188.20,189.80,52341000\n"
      "2026-02-11,190.00,191.20,189.30,190.55,48127000\n"
      "2026-02-12,190.60,192.00,190.10,191.75,55890000\n"
  )


  @pytest.fixture
  def csv_path(tmp_path: Path) -> Path:
      p = tmp_path / "qc_price_history.csv"
      p.write_text(CSV_CONTENT)
      return p


  def test_iter_bars_yields_each_row_in_order(csv_path: Path) -> None:
      reader = FixtureDataReader(csv_path)
      bars = list(reader.iter_bars(symbol="AAPL"))

      assert len(bars) == 3
      assert bars[0].open == Decimal("189.50")
      assert bars[0].close == Decimal("189.80")
      assert bars[1].open == Decimal("190.00")
      assert bars[2].close == Decimal("191.75")


  def test_iter_bars_filters_by_date_range(csv_path: Path) -> None:
      reader = FixtureDataReader(csv_path)
      bars = list(
          reader.iter_bars(
              symbol="AAPL",
              start=date(2026, 2, 11),
              end=date(2026, 2, 11),
          )
      )
      assert len(bars) == 1
      assert bars[0].open == Decimal("190.00")


  def test_iter_bars_unknown_symbol_returns_empty(csv_path: Path) -> None:
      reader = FixtureDataReader(csv_path, symbol="AAPL")
      bars = list(reader.iter_bars(symbol="MSFT"))
      assert bars == []
  ```

- [ ] **Step 2.3** Run test, confirm failure

  ```
  podman exec polygon-data-service python -m pytest /app/tests/research/parity/test_fixture_data_reader.py -v
  ```
  Expected: `ModuleNotFoundError` on `app.research.parity.fixture_data_reader`.

- [ ] **Step 2.4** Implement `FixtureDataReader`

  `PythonDataService/app/research/parity/fixture_data_reader.py`:
  ```python
  """CSV-backed data reader for offline parity tests.

  Reads a daily-OHLCV CSV (the QC `qb.history` export shape) and yields
  bars in the engine's TradeBar shape. Stores ``Decimal`` prices to
  preserve fixture precision.
  """

  from __future__ import annotations

  from dataclasses import dataclass
  from datetime import date, datetime, timezone
  from decimal import Decimal
  from pathlib import Path
  from typing import Iterator

  import pandas as pd


  @dataclass(frozen=True)
  class FixtureBar:
      symbol: str
      timestamp_ms: int
      open: Decimal
      high: Decimal
      low: Decimal
      close: Decimal
      volume: int

      @property
      def trading_date(self) -> date:
          return datetime.fromtimestamp(self.timestamp_ms / 1000, tz=timezone.utc).date()


  class FixtureDataReader:
      """Yields ``FixtureBar`` records from a CSV fixture.

      The CSV must have columns ``time,open,high,low,close,volume``. ``time``
      is parsed as a calendar date and anchored to 21:00 UTC (16:00 ET) — the
      same convention used by the QC daily export.
      """

      def __init__(self, csv_path: Path, *, symbol: str = "AAPL") -> None:
          self._csv_path = Path(csv_path)
          self._default_symbol = symbol
          self._frame = self._load(self._csv_path)

      @staticmethod
      def _load(path: Path) -> pd.DataFrame:
          frame = pd.read_csv(path, dtype={"volume": "int64"})
          frame["time"] = pd.to_datetime(frame["time"], utc=False).dt.date
          return frame.sort_values("time").reset_index(drop=True)

      def iter_bars(
          self,
          *,
          symbol: str,
          start: date | None = None,
          end: date | None = None,
      ) -> Iterator[FixtureBar]:
          if symbol != self._default_symbol:
              return iter(())

          frame = self._frame
          if start is not None:
              frame = frame[frame["time"] >= start]
          if end is not None:
              frame = frame[frame["time"] <= end]

          out: list[FixtureBar] = []
          for row in frame.itertuples(index=False):
              row_dt = datetime.combine(row.time, datetime.min.time(), tzinfo=timezone.utc)
              ts_ms = int(row_dt.timestamp() * 1000) + (21 * 3600 * 1000)
              out.append(
                  FixtureBar(
                      symbol=symbol,
                      timestamp_ms=ts_ms,
                      open=Decimal(str(row.open)),
                      high=Decimal(str(row.high)),
                      low=Decimal(str(row.low)),
                      close=Decimal(str(row.close)),
                      volume=int(row.volume),
                  )
              )
          return iter(out)
  ```

- [ ] **Step 2.5** Run test, confirm pass

  ```
  podman exec polygon-data-service python -m pytest /app/tests/research/parity/test_fixture_data_reader.py -v
  ```

- [ ] **Step 2.6** Commit

  ```
  git add PythonDataService/app/research/parity/fixture_data_reader.py \
          PythonDataService/tests/research/parity/test_fixture_data_reader.py
  git commit -m "feat(parity): FixtureDataReader for CSV-backed daily-bar replay"
  ```

---

## Task 3: `IbkrEquityCommissionModel` (standalone)

**Files:**
- Create: `PythonDataService/app/research/parity/ibkr_commission.py`
- Test: `PythonDataService/tests/research/parity/test_ibkr_commission.py`

**Reference:** QC IBKR brokerage model docs (linked in `attribution.md`). Equity tier:
- Per share: $0.005
- Minimum per order: $1.00
- Maximum per order: 0.5% of trade value

- [ ] **Step 3.1** Write failing test

  `tests/research/parity/test_ibkr_commission.py`:
  ```python
  from __future__ import annotations

  from decimal import Decimal

  import pytest

  from app.research.parity.ibkr_commission import IbkrEquityCommissionModel


  @pytest.fixture
  def model() -> IbkrEquityCommissionModel:
      return IbkrEquityCommissionModel()


  def test_small_order_hits_minimum(model: IbkrEquityCommissionModel) -> None:
      # 100 shares * $0.005 = $0.50 → bumped up to $1.00 minimum.
      fee = model.fee(quantity=100, fill_price=Decimal("150.00"))
      assert fee == Decimal("1.00")


  def test_large_order_uses_per_share(model: IbkrEquityCommissionModel) -> None:
      # 1_000 shares * $0.005 = $5.00; 0.5% of 150_000 = $750. Use per-share.
      fee = model.fee(quantity=1_000, fill_price=Decimal("150.00"))
      assert fee == Decimal("5.00")


  def test_tiny_value_caps_at_half_percent(model: IbkrEquityCommissionModel) -> None:
      # 500 shares * $0.005 = $2.50; 0.5% of (500*0.10)=$50 → $0.25 cap < $2.50.
      fee = model.fee(quantity=500, fill_price=Decimal("0.10"))
      assert fee == Decimal("0.25")


  def test_negative_quantity_treated_as_absolute(model: IbkrEquityCommissionModel) -> None:
      # Short / sell side: absolute quantity drives the fee.
      assert model.fee(quantity=-1_000, fill_price=Decimal("150.00")) == Decimal("5.00")
  ```

- [ ] **Step 3.2** Run, confirm failure (`ModuleNotFoundError`).

- [ ] **Step 3.3** Implement

  `PythonDataService/app/research/parity/ibkr_commission.py`:
  ```python
  """IBKR equity-tier commission model used by the QC reconciler.

  Standalone helper — not wired into the backtest engine. The reconciler
  invokes this to compute the expected per-fill fee and compares it to
  QC's recorded ``orderFeeAmount``.
  """

  from __future__ import annotations

  from dataclasses import dataclass
  from decimal import ROUND_HALF_UP, Decimal


  _PER_SHARE_DEFAULT = Decimal("0.005")
  _MIN_PER_ORDER = Decimal("1.00")
  _MAX_PCT_OF_VALUE = Decimal("0.005")  # 0.5%
  _CENT = Decimal("0.01")


  @dataclass(frozen=True)
  class IbkrEquityCommissionModel:
      """QC's documented IBKR equity brokerage commission model."""

      per_share: Decimal = _PER_SHARE_DEFAULT
      min_per_order: Decimal = _MIN_PER_ORDER
      max_pct_of_value: Decimal = _MAX_PCT_OF_VALUE

      def fee(self, *, quantity: int, fill_price: Decimal) -> Decimal:
          shares = Decimal(abs(int(quantity)))
          trade_value = shares * fill_price
          raw_per_share = (shares * self.per_share).quantize(_CENT, rounding=ROUND_HALF_UP)
          fee = max(self.min_per_order, raw_per_share)
          cap = (trade_value * self.max_pct_of_value).quantize(_CENT, rounding=ROUND_HALF_UP)
          return min(fee, cap) if cap > Decimal("0") else fee
  ```

- [ ] **Step 3.4** Run, confirm pass.

- [ ] **Step 3.5** Commit
  ```
  git add PythonDataService/app/research/parity/ibkr_commission.py \
          PythonDataService/tests/research/parity/test_ibkr_commission.py
  git commit -m "feat(parity): IbkrEquityCommissionModel for reconciler-side fee parity"
  ```

---

## Task 4: `QcReconciler` core dataclasses

**Files:**
- Create: `PythonDataService/app/research/parity/qc_reconciler.py` (initial dataclasses + module skeleton)
- Test: `PythonDataService/tests/research/parity/test_qc_reconciler.py` (orders parser only — first half)

- [ ] **Step 4.1** Write failing test for `_parse_qc_orders`

  ```python
  from __future__ import annotations

  import json
  from decimal import Decimal
  from pathlib import Path

  import pytest

  from app.research.parity.qc_reconciler import _parse_qc_orders


  @pytest.fixture
  def qc_orders_path(tmp_path: Path) -> Path:
      payload = {
          "orders": [
              {
                  "id": 1,
                  "symbol": "AAPL",
                  "type": 0,                  # 0 == Market in QC's enum
                  "direction": 0,             # 0 == Buy
                  "quantity": 526,
                  "events": [
                      {
                          "time": "2026-02-11T13:30:00Z",
                          "fillQuantity": 526,
                          "fillPrice": 190.00,
                          "direction": 0,
                          "orderFeeAmount": 2.63,
                      }
                  ],
              },
              {
                  "id": 2,
                  "symbol": "AAPL",
                  "type": 0,
                  "direction": 1,             # 1 == Sell
                  "quantity": -526,
                  "events": [
                      {
                          "time": "2026-02-25T13:30:00Z",
                          "fillQuantity": -526,
                          "fillPrice": 195.20,
                          "direction": 1,
                          "orderFeeAmount": 2.63,
                      }
                  ],
              },
          ]
      }
      p = tmp_path / "qc_orders.json"
      p.write_text(json.dumps(payload))
      return p


  def test_parse_qc_orders_extracts_one_fill_per_event(qc_orders_path: Path) -> None:
      fills = _parse_qc_orders(qc_orders_path)

      assert len(fills) == 2
      assert fills[0].symbol == "AAPL"
      assert fills[0].side == "buy"
      assert fills[0].fill_qty == 526
      assert fills[0].fill_price == Decimal("190.00")
      assert fills[0].fee == Decimal("2.63")
      assert fills[1].side == "sell"
      assert fills[1].fill_qty == -526
  ```

- [ ] **Step 4.2** Run, confirm failure.

- [ ] **Step 4.3** Implement skeleton + `_parse_qc_orders`

  `PythonDataService/app/research/parity/qc_reconciler.py`:
  ```python
  """QC reconciler — diff QC's recorded backtest against our trade log.

  Public entry point: ``reconcile_qc_aapl_phase3``.

  Implementation is intentionally split into private functions so each step
  is unit-testable. See ``docs/superpowers/specs/2026-05-11-phase3-pnl-parity-design.md``.
  """

  from __future__ import annotations

  import json
  from dataclasses import dataclass, field
  from datetime import date, datetime, timezone
  from decimal import Decimal
  from enum import Enum
  from pathlib import Path
  from typing import Any, Literal


  Side = Literal["buy", "sell"]


  class DivergenceCategory(str, Enum):
      FIXTURE_INSUFFICIENT = "fixture_insufficient"
      DECISION_MISMATCH = "decision_mismatch"
      DIRECTION_MISMATCH = "direction_mismatch"
      QUANTITY_MISMATCH = "quantity_mismatch"
      FILL_PRICE_DRIFT = "fill_price_drift"
      COMMISSION_DRIFT = "commission_drift"
      PNL_DRIFT = "pnl_drift"
      ORDER_TYPE_MISMATCH = "order_type_mismatch"


  @dataclass(frozen=True)
  class Tolerances:
      fill_price_atol: Decimal = Decimal("0.01")
      commission_atol: Decimal = Decimal("0.01")
      per_share_pnl_atol: Decimal = Decimal("0.01")
      pnl_floor_atol: Decimal = Decimal("0.01")

      @classmethod
      def phase3_default(cls) -> "Tolerances":
          return cls()


  @dataclass(frozen=True)
  class QcFill:
      order_id: int
      symbol: str
      side: Side
      fill_qty: int
      fill_price: Decimal
      fill_time_ms: int
      fee: Decimal | None
      order_type_code: int

      @property
      def trading_date(self) -> date:
          return datetime.fromtimestamp(self.fill_time_ms / 1000, tz=timezone.utc).date()


  @dataclass(frozen=True)
  class OurFill:
      symbol: str
      side: Side
      fill_qty: int
      fill_price: Decimal
      fill_time_ms: int
      fee: Decimal

      @property
      def trading_date(self) -> date:
          return datetime.fromtimestamp(self.fill_time_ms / 1000, tz=timezone.utc).date()


  @dataclass(frozen=True)
  class FixtureAudit:
      qc_fill: QcFill
      reason: str
      expected_open: Decimal | None
      actual_fill_price: Decimal


  @dataclass(frozen=True)
  class ReconciledPair:
      qc: QcFill | None
      ours: OurFill | None
      trading_date: date
      side: Side | None


  @dataclass(frozen=True)
  class Divergence:
      category: DivergenceCategory
      pair: ReconciledPair
      detail: str


  @dataclass(frozen=True)
  class ReconciliationSummary:
      n_pairs: int
      n_qc_fills: int
      n_our_fills: int
      n_unmatched_qc: int
      n_unmatched_ours: int
      n_divergences_by_category: dict[DivergenceCategory, int]


  @dataclass(frozen=True)
  class Diagnostics:
      computed_ibkr_fees: dict[int, Decimal] = field(default_factory=dict)
      propagated_pnl_atol: Decimal = Decimal("0")


  @dataclass(frozen=True)
  class FixtureMetadata:
      qc_orders_path: Path
      qc_price_history_path: Path
      window_start: date | None
      window_end: date | None


  @dataclass(frozen=True)
  class ReconciliationReport:
      status: Literal["passed", "failed"]
      summary: ReconciliationSummary
      tolerances: Tolerances
      fixture_audit: list[FixtureAudit]
      pairs: list[ReconciledPair]
      divergences: list[Divergence]
      diagnostics: Diagnostics
      fixture_metadata: FixtureMetadata

      def render_markdown(self) -> str:
          lines: list[str] = []
          lines.append(f"# QC AAPL Phase 3 reconciliation report — {self.status.upper()}")
          lines.append("")
          lines.append("## Summary")
          s = self.summary
          lines.append(f"- Pairs: {s.n_pairs}")
          lines.append(f"- QC fills: {s.n_qc_fills}; ours: {s.n_our_fills}")
          lines.append(f"- Unmatched QC: {s.n_unmatched_qc}; unmatched ours: {s.n_unmatched_ours}")
          lines.append(f"- Propagated PnL atol: {self.diagnostics.propagated_pnl_atol}")
          for cat, n in s.n_divergences_by_category.items():
              lines.append(f"  - {cat.value}: {n}")
          if self.divergences:
              lines.append("")
              lines.append("## Divergences")
              for d in self.divergences:
                  lines.append(f"- [{d.category.value}] {d.detail}")
          if self.fixture_audit:
              lines.append("")
              lines.append("## Fixture audit failures")
              for fa in self.fixture_audit:
                  lines.append(f"- order trading_date {fa.qc_fill.trading_date}: {fa.reason}")
          return "\n".join(lines) + "\n"

      def render_json(self) -> dict[str, Any]:
          return {
              "status": self.status,
              "summary": {
                  "n_pairs": self.summary.n_pairs,
                  "n_qc_fills": self.summary.n_qc_fills,
                  "n_our_fills": self.summary.n_our_fills,
                  "n_unmatched_qc": self.summary.n_unmatched_qc,
                  "n_unmatched_ours": self.summary.n_unmatched_ours,
                  "n_divergences_by_category": {
                      k.value: v for k, v in self.summary.n_divergences_by_category.items()
                  },
              },
              "divergence_count": len(self.divergences),
              "fixture_audit_count": len(self.fixture_audit),
              "propagated_pnl_atol": str(self.diagnostics.propagated_pnl_atol),
          }


  def _parse_qc_orders(path: Path) -> list[QcFill]:
      payload = json.loads(Path(path).read_text())
      raw_orders = payload.get("orders") or payload  # tolerate both shapes
      fills: list[QcFill] = []
      for order in raw_orders:
          symbol = order["symbol"].split(" ", 1)[0]  # strip QC security-id suffix
          order_type_code = int(order.get("type", 0))
          for event in order.get("events", []):
              fill_qty = int(event["fillQuantity"])
              side: Side = "buy" if fill_qty > 0 else "sell"
              fill_time = event["time"]
              fill_time_dt = datetime.fromisoformat(fill_time.replace("Z", "+00:00"))
              fee_raw = event.get("orderFeeAmount")
              fills.append(
                  QcFill(
                      order_id=int(order["id"]),
                      symbol=symbol,
                      side=side,
                      fill_qty=fill_qty,
                      fill_price=Decimal(str(event["fillPrice"])),
                      fill_time_ms=int(fill_time_dt.timestamp() * 1000),
                      fee=None if fee_raw is None else Decimal(str(fee_raw)),
                      order_type_code=order_type_code,
                  )
              )
      return fills
  ```

- [ ] **Step 4.4** Run test, confirm pass.

- [ ] **Step 4.5** Commit
  ```
  git add PythonDataService/app/research/parity/qc_reconciler.py \
          PythonDataService/tests/research/parity/test_qc_reconciler.py
  git commit -m "feat(parity): QcReconciler dataclasses + qc-orders parser"
  ```

---

## Task 5: Reconciler — fixture audit, alignment, classification, public entry point

**Files:**
- Modify: `PythonDataService/app/research/parity/qc_reconciler.py`
- Test: extend `PythonDataService/tests/research/parity/test_qc_reconciler.py`

- [ ] **Step 5.1** Add tests for `_audit_fixture`

  Append to `test_qc_reconciler.py`:
  ```python
  from app.research.parity.fixture_data_reader import FixtureDataReader
  from app.research.parity.qc_reconciler import (
      _align_fills,
      _audit_fixture,
      _classify_divergences,
      DivergenceCategory,
      OurFill,
      QcFill,
      Tolerances,
  )


  def _qc_fill(side: str, qty: int, price: str, day: str, fee: str = "1.00") -> QcFill:
      from datetime import datetime, timezone
      from decimal import Decimal
      dt = datetime.fromisoformat(f"{day}T13:30:00+00:00")
      return QcFill(
          order_id=1,
          symbol="AAPL",
          side=side,  # type: ignore[arg-type]
          fill_qty=qty,
          fill_price=Decimal(price),
          fill_time_ms=int(dt.timestamp() * 1000),
          fee=Decimal(fee),
          order_type_code=0,
      )


  def _our_fill(side: str, qty: int, price: str, day: str, fee: str = "0.00") -> OurFill:
      from datetime import datetime, timezone
      from decimal import Decimal
      dt = datetime.fromisoformat(f"{day}T21:00:00+00:00")
      return OurFill(
          symbol="AAPL",
          side=side,  # type: ignore[arg-type]
          fill_qty=qty,
          fill_price=Decimal(price),
          fill_time_ms=int(dt.timestamp() * 1000),
          fee=Decimal(fee),
      )


  def test_audit_fixture_passes_when_fill_explained_by_next_open(tmp_path) -> None:
      csv = tmp_path / "bars.csv"
      csv.write_text(
          "time,open,high,low,close,volume\n"
          "2026-02-10,189.50,190.10,188.20,189.80,52341000\n"
          "2026-02-11,190.00,191.20,189.30,190.55,48127000\n"
      )
      reader = FixtureDataReader(csv)
      qc = [_qc_fill("buy", 526, "190.00", "2026-02-11")]
      audits = _audit_fixture(qc, reader, Tolerances.phase3_default())
      assert audits == []


  def test_audit_fixture_flags_unexplained_price(tmp_path) -> None:
      csv = tmp_path / "bars.csv"
      csv.write_text(
          "time,open,high,low,close,volume\n"
          "2026-02-10,189.50,190.10,188.20,189.80,52341000\n"
          "2026-02-11,190.00,191.20,189.30,190.55,48127000\n"
      )
      reader = FixtureDataReader(csv)
      qc = [_qc_fill("buy", 526, "195.55", "2026-02-11")]  # nowhere near $190 open
      audits = _audit_fixture(qc, reader, Tolerances.phase3_default())
      assert len(audits) == 1


  def test_align_fills_pairs_by_date_and_side() -> None:
      qc = [
          _qc_fill("buy", 526, "190.00", "2026-02-11"),
          _qc_fill("sell", -526, "195.20", "2026-02-25"),
      ]
      ours = [
          _our_fill("buy", 526, "190.01", "2026-02-11"),
          _our_fill("sell", -526, "195.19", "2026-02-25"),
      ]
      pairs = _align_fills(qc, ours)
      assert len(pairs) == 2
      assert pairs[0].qc is not None and pairs[0].ours is not None
      assert pairs[0].trading_date.isoformat() == "2026-02-11"


  def test_align_fills_emits_unmatched_pair_when_one_side_missing() -> None:
      qc = [_qc_fill("buy", 526, "190.00", "2026-02-11")]
      ours: list = []
      pairs = _align_fills(qc, ours)
      assert len(pairs) == 1
      assert pairs[0].ours is None


  def test_classify_divergences_flags_price_drift_above_tolerance() -> None:
      pair_qc = _qc_fill("buy", 526, "190.00", "2026-02-11")
      pair_ours = _our_fill("buy", 526, "190.50", "2026-02-11")  # 50c drift
      pairs = _align_fills([pair_qc], [pair_ours])
      divs = _classify_divergences(pairs, Tolerances.phase3_default(), assert_fees=False)
      kinds = {d.category for d in divs}
      assert DivergenceCategory.FILL_PRICE_DRIFT in kinds


  def test_classify_divergences_flags_quantity_mismatch() -> None:
      pair_qc = _qc_fill("buy", 526, "190.00", "2026-02-11")
      pair_ours = _our_fill("buy", 500, "190.00", "2026-02-11")
      pairs = _align_fills([pair_qc], [pair_ours])
      divs = _classify_divergences(pairs, Tolerances.phase3_default(), assert_fees=False)
      kinds = {d.category for d in divs}
      assert DivergenceCategory.QUANTITY_MISMATCH in kinds


  def test_classify_divergences_unmatched_emits_decision_mismatch() -> None:
      pair_qc = _qc_fill("buy", 526, "190.00", "2026-02-11")
      pairs = _align_fills([pair_qc], [])
      divs = _classify_divergences(pairs, Tolerances.phase3_default(), assert_fees=False)
      kinds = {d.category for d in divs}
      assert DivergenceCategory.DECISION_MISMATCH in kinds
  ```

- [ ] **Step 5.2** Run tests, confirm failures (`ImportError` for `_audit_fixture`/`_align_fills`/`_classify_divergences`).

- [ ] **Step 5.3** Add helper functions + public entry point to `qc_reconciler.py`

  Append:
  ```python
  from app.research.parity.fixture_data_reader import FixtureDataReader, FixtureBar
  from app.research.parity.ibkr_commission import IbkrEquityCommissionModel


  def _bars_by_date(reader: FixtureDataReader, symbol: str) -> dict[date, FixtureBar]:
      return {bar.trading_date: bar for bar in reader.iter_bars(symbol=symbol)}


  def _audit_fixture(
      qc_fills: list[QcFill],
      reader: FixtureDataReader,
      tolerances: Tolerances,
  ) -> list[FixtureAudit]:
      bars = _bars_by_date(reader, symbol="AAPL")
      audits: list[FixtureAudit] = []
      for qc in qc_fills:
          bar = bars.get(qc.trading_date)
          if bar is None:
              audits.append(
                  FixtureAudit(
                      qc_fill=qc,
                      reason=f"no bar for {qc.trading_date}",
                      expected_open=None,
                      actual_fill_price=qc.fill_price,
                  )
              )
              continue
          if abs(bar.open - qc.fill_price) > tolerances.fill_price_atol:
              audits.append(
                  FixtureAudit(
                      qc_fill=qc,
                      reason=(
                          f"fill {qc.fill_price} not explained by bar open {bar.open} "
                          f"(tolerance {tolerances.fill_price_atol})"
                      ),
                      expected_open=bar.open,
                      actual_fill_price=qc.fill_price,
                  )
              )
      return audits


  def _align_fills(
      qc_fills: list[QcFill],
      our_fills: list[OurFill],
  ) -> list[ReconciledPair]:
      key = lambda side, d: (side, d)  # noqa: E731
      qc_map: dict[tuple[Side, date], QcFill] = {(f.side, f.trading_date): f for f in qc_fills}
      ours_map: dict[tuple[Side, date], OurFill] = {(f.side, f.trading_date): f for f in our_fills}
      all_keys = set(qc_map) | set(ours_map)
      pairs: list[ReconciledPair] = []
      for k in sorted(all_keys, key=lambda x: (x[1], x[0])):
          side, day = k
          pairs.append(
              ReconciledPair(qc=qc_map.get(k), ours=ours_map.get(k), trading_date=day, side=side)
          )
      return pairs


  def _classify_divergences(
      pairs: list[ReconciledPair],
      tolerances: Tolerances,
      *,
      assert_fees: bool,
      computed_ibkr_fees: dict[int, Decimal] | None = None,
  ) -> list[Divergence]:
      out: list[Divergence] = []
      computed_ibkr_fees = computed_ibkr_fees or {}
      for pair in pairs:
          if pair.qc is None or pair.ours is None:
              out.append(
                  Divergence(
                      category=DivergenceCategory.DECISION_MISMATCH,
                      pair=pair,
                      detail=(
                          f"only one side has a fill on {pair.trading_date} ({pair.side})"
                      ),
                  )
              )
              continue
          qc, ours = pair.qc, pair.ours
          if qc.side != ours.side:
              out.append(
                  Divergence(
                      category=DivergenceCategory.DIRECTION_MISMATCH,
                      pair=pair,
                      detail=f"qc={qc.side} ours={ours.side}",
                  )
              )
          if qc.fill_qty != ours.fill_qty:
              out.append(
                  Divergence(
                      category=DivergenceCategory.QUANTITY_MISMATCH,
                      pair=pair,
                      detail=f"qc qty={qc.fill_qty} ours qty={ours.fill_qty}",
                  )
              )
          if abs(qc.fill_price - ours.fill_price) > tolerances.fill_price_atol:
              out.append(
                  Divergence(
                      category=DivergenceCategory.FILL_PRICE_DRIFT,
                      pair=pair,
                      detail=(
                          f"|{qc.fill_price} - {ours.fill_price}| > {tolerances.fill_price_atol}"
                      ),
                  )
              )
          if assert_fees and qc.fee is not None:
              expected = computed_ibkr_fees.get(qc.order_id)
              if expected is not None and abs(qc.fee - expected) > tolerances.commission_atol:
                  out.append(
                      Divergence(
                          category=DivergenceCategory.COMMISSION_DRIFT,
                          pair=pair,
                          detail=f"qc fee={qc.fee} expected ibkr={expected}",
                      )
                  )
          if qc.order_type_code != 0:
              out.append(
                  Divergence(
                      category=DivergenceCategory.ORDER_TYPE_MISMATCH,
                      pair=pair,
                      detail=f"qc order_type={qc.order_type_code} (expected market=0)",
                  )
              )
      return out


  def reconcile_qc_aapl_phase3(
      *,
      qc_orders_path: Path,
      qc_price_history_path: Path,
      our_fills: list[OurFill],
      tolerances: Tolerances | None = None,
      assert_fees: bool = False,
  ) -> ReconciliationReport:
      tolerances = tolerances or Tolerances.phase3_default()
      qc_fills = _parse_qc_orders(qc_orders_path)
      reader = FixtureDataReader(qc_price_history_path)
      audit = _audit_fixture(qc_fills, reader, tolerances)

      commission_model = IbkrEquityCommissionModel()
      computed_fees: dict[int, Decimal] = {
          qf.order_id: commission_model.fee(quantity=qf.fill_qty, fill_price=qf.fill_price)
          for qf in qc_fills
      }

      pairs = _align_fills(qc_fills, our_fills)
      divergences: list[Divergence] = []
      if audit:
          # If the fixture itself doesn't explain QC's fills, halt — alignment
          # results would be misleading.
          for fa in audit:
              divergences.append(
                  Divergence(
                      category=DivergenceCategory.FIXTURE_INSUFFICIENT,
                      pair=ReconciledPair(
                          qc=fa.qc_fill,
                          ours=None,
                          trading_date=fa.qc_fill.trading_date,
                          side=fa.qc_fill.side,
                      ),
                      detail=fa.reason,
                  )
              )
      else:
          divergences = _classify_divergences(
              pairs,
              tolerances,
              assert_fees=assert_fees,
              computed_ibkr_fees=computed_fees,
          )

      # Propagated PnL atol: Σ |fill_qty_i| × $0.01 + Σ fee_atol_i.
      total_qty = sum(abs(f.fill_qty) for f in qc_fills)
      n_fills = len(qc_fills)
      propagated = (
          Decimal(total_qty) * tolerances.per_share_pnl_atol
          + Decimal(n_fills) * tolerances.commission_atol
      ) if n_fills else Decimal("0")

      counts: dict[DivergenceCategory, int] = {}
      for d in divergences:
          counts[d.category] = counts.get(d.category, 0) + 1
      summary = ReconciliationSummary(
          n_pairs=len(pairs),
          n_qc_fills=len(qc_fills),
          n_our_fills=len(our_fills),
          n_unmatched_qc=sum(1 for p in pairs if p.qc is not None and p.ours is None),
          n_unmatched_ours=sum(1 for p in pairs if p.qc is None and p.ours is not None),
          n_divergences_by_category=counts,
      )
      gating_categories = {
          DivergenceCategory.FIXTURE_INSUFFICIENT,
          DivergenceCategory.DECISION_MISMATCH,
          DivergenceCategory.DIRECTION_MISMATCH,
          DivergenceCategory.QUANTITY_MISMATCH,
          DivergenceCategory.FILL_PRICE_DRIFT,
          DivergenceCategory.ORDER_TYPE_MISMATCH,
          DivergenceCategory.PNL_DRIFT,
      }
      if assert_fees:
          gating_categories.add(DivergenceCategory.COMMISSION_DRIFT)
      status: Literal["passed", "failed"] = (
          "passed" if not any(d.category in gating_categories for d in divergences) else "failed"
      )

      metadata = FixtureMetadata(
          qc_orders_path=Path(qc_orders_path),
          qc_price_history_path=Path(qc_price_history_path),
          window_start=min((f.trading_date for f in qc_fills), default=None),
          window_end=max((f.trading_date for f in qc_fills), default=None),
      )
      diagnostics = Diagnostics(
          computed_ibkr_fees=computed_fees,
          propagated_pnl_atol=propagated,
      )
      return ReconciliationReport(
          status=status,
          summary=summary,
          tolerances=tolerances,
          fixture_audit=audit,
          pairs=pairs,
          divergences=divergences,
          diagnostics=diagnostics,
          fixture_metadata=metadata,
      )
  ```

- [ ] **Step 5.4** Run, confirm pass.

- [ ] **Step 5.5** Commit
  ```
  git add PythonDataService/app/research/parity/qc_reconciler.py \
          PythonDataService/tests/research/parity/test_qc_reconciler.py
  git commit -m "feat(parity): QcReconciler audit + alignment + classification"
  ```

---

## Task 6: Pytest `--write-recon-report` flag

**Files:** Create `PythonDataService/tests/research/parity/conftest.py`

- [ ] **Step 6.1** Write conftest

  ```python
  """Phase 3+ parity test harness — global pytest options."""

  from __future__ import annotations

  import pytest


  def pytest_addoption(parser: pytest.Parser) -> None:
      parser.addoption(
          "--write-recon-report",
          action="store_true",
          default=False,
          help=(
              "Write reconciliation report to artifacts/reconciliations/ "
              "even when the parity test passes (default: only on failure)."
          ),
      )


  @pytest.fixture
  def write_recon_report(request: pytest.FixtureRequest) -> bool:
      return bool(request.config.getoption("--write-recon-report"))
  ```

- [ ] **Step 6.2** Commit

  ```
  git add PythonDataService/tests/research/parity/conftest.py
  git commit -m "feat(parity): --write-recon-report pytest flag for phase 3 harness"
  ```

---

## Task 7: Skipped acceptance + smoke tests

**Files:**
- Create: `PythonDataService/tests/research/parity/test_qc_fixture_smoke.py`
- Create: `PythonDataService/tests/research/parity/test_qc_aapl_phase3_trade_parity.py`

These tests are skipped on master (fixture not committed yet) but become live the moment the fixture lands. They are part of this PR so the contract is enforced from day 1.

- [ ] **Step 7.1** Write `test_qc_fixture_smoke.py`

  ```python
  """Phase 3 capture-smoke: validate the QC fixture shape once it lands.

  Skipped on master until ``tests/fixtures/golden/qc-aapl-phase3/`` is committed.
  """

  from __future__ import annotations

  import json
  from pathlib import Path

  import pytest


  _FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "golden" / "qc-aapl-phase3"
  _ORDERS = _FIXTURE_DIR / "qc_orders.json"
  _PRICES = _FIXTURE_DIR / "qc_price_history.csv"
  _EQUITY = _FIXTURE_DIR / "qc_equity.json"


  pytestmark = pytest.mark.skipif(
      not _ORDERS.is_file(),
      reason="Phase 3 fixture not yet captured; see docs/superpowers/specs/2026-05-11-phase3-pnl-parity-design.md",
  )


  def test_orders_fixture_has_expected_event_fields() -> None:
      payload = json.loads(_ORDERS.read_text())
      raw = payload.get("orders") or payload
      assert raw, "qc_orders.json has no orders"
      sample = raw[0]
      assert "events" in sample and sample["events"]
      event = sample["events"][0]
      for key in ("time", "fillQuantity", "fillPrice", "direction"):
          assert key in event, f"event missing '{key}'"


  def test_orders_fixture_fee_presence_branch_decider(capsys: pytest.CaptureFixture[str]) -> None:
      payload = json.loads(_ORDERS.read_text())
      raw = payload.get("orders") or payload
      any_nonzero_fee = False
      for order in raw:
          for event in order.get("events", []):
              fee = event.get("orderFeeAmount")
              if fee is not None and float(fee) != 0.0:
                  any_nonzero_fee = True
                  break
      print(
          f"FEE_PRESENCE_BRANCH={'A' if any_nonzero_fee else 'B'} "
          f"(any non-zero orderFeeAmount in qc_orders.json = {any_nonzero_fee})"
      )
      assert _ORDERS.is_file()  # smoke-only — never fail on branch identity


  def test_price_history_fixture_has_daily_ohlcv() -> None:
      lines = _PRICES.read_text().splitlines()
      assert lines[0].strip().lower() == "time,open,high,low,close,volume"
      assert len(lines) > 1, "qc_price_history.csv has no rows"


  def test_equity_fixture_parses() -> None:
      json.loads(_EQUITY.read_text())  # diagnostic only — just confirm valid JSON
  ```

- [ ] **Step 7.2** Write `test_qc_aapl_phase3_trade_parity.py`

  ```python
  """Phase 3 acceptance: AAPL single-symbol trade-level parity vs QC.

  Skipped on master until ``tests/fixtures/golden/qc-aapl-phase3/`` is committed.
  When live, builds the AAPL single-symbol spec, runs the engine with
  ``fill_mode="next_bar_open"`` and ``commission_per_order=0``, reconciles
  against ``qc_orders.json``, and asserts ``report.status == "passed"``.
  """

  from __future__ import annotations

  from pathlib import Path

  import pytest

  from app.research.parity.qc_reconciler import (
      OurFill,
      Tolerances,
      reconcile_qc_aapl_phase3,
  )


  _FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "golden" / "qc-aapl-phase3"
  _ORDERS = _FIXTURE_DIR / "qc_orders.json"
  _PRICES = _FIXTURE_DIR / "qc_price_history.csv"
  _ARTIFACTS_DIR = Path(__file__).resolve().parents[3] / "artifacts" / "reconciliations"


  pytestmark = pytest.mark.skipif(
      not _ORDERS.is_file(),
      reason="Phase 3 fixture not yet captured",
  )


  def _build_our_fills() -> list[OurFill]:
      """Run the AAPL spec through ``run_strategy_spec`` and normalize trades to fills.

      Filled in when the fixture lands — depends on the captured prediction-set
      window matching the orders window.
      """
      raise NotImplementedError(
          "Phase 3 fixture capture must precede this implementation. "
          "See docs/superpowers/specs/2026-05-11-phase3-pnl-parity-design.md §2.3"
      )


  def test_qc_aapl_phase3_trade_level_parity(
      tmp_path: Path,
      write_recon_report: bool,
  ) -> None:
      our_fills = _build_our_fills()
      report = reconcile_qc_aapl_phase3(
          qc_orders_path=_ORDERS,
          qc_price_history_path=_PRICES,
          our_fills=our_fills,
          tolerances=Tolerances.phase3_default(),
          assert_fees=False,  # flip to True only on Branch A after fixture review
      )
      if report.status != "passed" or write_recon_report:
          _ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
          (_ARTIFACTS_DIR / "qc-aapl-phase3-latest.md").write_text(report.render_markdown())
      assert report.status == "passed", f"reconciliation failed; report written to {_ARTIFACTS_DIR}"
  ```

- [ ] **Step 7.3** Commit
  ```
  git add PythonDataService/tests/research/parity/test_qc_fixture_smoke.py \
          PythonDataService/tests/research/parity/test_qc_aapl_phase3_trade_parity.py
  git commit -m "test(parity): skipped phase-3 smoke + acceptance tests (live on fixture commit)"
  ```

---

## Task 8: Numerical-rigor doc — trade-level reconciliation taxonomy

**Files:** Modify `.claude/rules/numerical-rigor.md`

- [ ] **Step 8.1** Append section

  At the end of `numerical-rigor.md`, before the "Sovereignty" section if present, add a new section titled **"Trade-level reconciliation taxonomy"** that enumerates the eight `DivergenceCategory` values, what each means, and which routes to a Phase 3.5 escalation vs. a Phase 3 engine fix.

- [ ] **Step 8.2** Commit
  ```
  git add .claude/rules/numerical-rigor.md
  git commit -m "docs(rules): trade-level reconciliation divergence taxonomy"
  ```

---

## Task 9: Run full tests + lint

- [ ] **Step 9.1** Run scoped pytest

  ```
  podman exec polygon-data-service python -m pytest /app/tests/research/parity -v
  ```
  Expected: all non-skipped tests pass; the two fixture-gated tests are skipped.

- [ ] **Step 9.2** Run ruff at project scope

  ```
  ruff check PythonDataService/app/ PythonDataService/tests/
  ```
  Expected: zero warnings/errors. If anything outside `app/research/parity/` flags, surface (don't auto-fix unrelated code).

- [ ] **Step 9.3** Run full pytest suite to detect cross-file drift

  ```
  podman exec polygon-data-service python -m pytest /app/tests -x -q
  ```
  Expected: baseline-equivalent — no new failures introduced by these changes.

---

## Task 10: PR + summary doc

- [ ] **Step 10.1** Push branch
  ```
  git push -u origin feat/phase3-pnl-parity
  ```

- [ ] **Step 10.2** Open PR
  ```
  gh pr create --title "feat(parity): Phase 3 — QC AAPL trade-level P&L parity scaffolding" \
               --body "..."
  ```

- [ ] **Step 10.3** Write `docs/handoffs/2026-05-11-phase3-implementation-summary.md`

  Section A: what shipped (the eight components + tests). Section B: what was *not* shipped this PR (the QC fixture itself, the IBKR engine wiring, the `_build_our_fills` body, Phase 4 multi-symbol ranking). Section C: how to activate the live test once the fixture is captured.
