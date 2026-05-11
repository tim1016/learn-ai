# QC AAPL Phase 3 fixture-capture runbook

Hand-run in QC Cloud to produce ``tests/fixtures/golden/qc-aapl-phase3/``. After capture, drop the three files into that directory and the two fixture-gated tests activate automatically.

## Inputs you need before opening QC

- Validation window: **2026-02-10 → 2026-03-12** (must match PR #215's prediction-set window)
- Backtest cash: **$100,000**
- Symbol: **AAPL** (single-symbol degenerate of QC's full SP500 universe)

## QC algorithm code

Identical to QC's "Precomputed ML Predictions" tutorial *except* for one line in ``_select_assets``. Paste this into a new algorithm:

```python
# algorithm.py — QC AAPL Phase 3 capture
from AlgorithmImports import *

class PrecomputedMlPredictionsAapl(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 2, 10)
        self.set_end_date(2026, 3, 12)
        self.set_cash(100_000)
        self.universe_settings.resolution = Resolution.DAILY

        # Phase 3 modification: AAPL-only universe.
        self.symbol_aapl = self.add_equity("AAPL", Resolution.DAILY).symbol
        self.add_universe(self._select_assets)

        self.schedule.on(
            self.date_rules.every_day(self.symbol_aapl),
            self.time_rules.at(8, 0),
            self._rebalance,
        )

        # Load predictions from research-to-backtest-factors.json
        # exactly as the tutorial does. (Object-store key unchanged.)
        self.predictions = self._load_predictions()

    def _select_assets(self, data):
        # Phase 3 override: pin universe to AAPL.
        return [self.symbol_aapl]

    def _rebalance(self):
        pred = self.predictions.get(str(self.time.date()), {}).get("AAPL")
        if pred is None:
            return
        target = 1.0 if pred > 0 else 0.0
        self.set_holdings(self.symbol_aapl, target)

    def _load_predictions(self):
        # Read research-to-backtest-factors.json from QC's object store
        # — same code as the tutorial's read cell.
        import json
        raw = self.object_store.read("research-to-backtest-factors.json")
        return json.loads(raw)
```

> **Audit-trail step.** Before running the backtest, take a screenshot of the entire algorithm file and save as ``qc_algorithm_screenshot.png``. We need this to prove what was actually executed (QC's editor can rebuild from cache and a single character change later would invalidate the fixture).

## Step 1 — run the backtest

Click **Build → Backtest**. Wait for it to complete. Record the backtest id from the URL (used in step 2).

## Step 2 — pull qc_orders.json

In a new QC Research notebook **bound to the same project as the algorithm**:

```python
from QuantConnect.Api import Api
import json, os

# Replace these — project id is in the project URL; backtest id is in the
# completed-backtest URL.
PROJECT_ID = 12345
BACKTEST_ID = "abcdef1234567890"

api = Api()
api.initialize(self.config.UserId, self.config.UserToken)

orders = api.read_backtest_orders(PROJECT_ID, BACKTEST_ID).orders
qc_orders_json = json.dumps(
    {"orders": [o.__dict__ for o in orders]},
    default=str,
    indent=2,
)
with open("qc_orders.json", "w") as f:
    f.write(qc_orders_json)
print(f"wrote {len(orders)} orders")
```

> If ``api.read_backtest_orders`` doesn't exist on your QC client version, use the equivalent ``/backtests/orders/read`` HTTP endpoint via ``requests.post(...)``.

## Step 3 — pull qc_price_history.csv

Same notebook:

```python
import pandas as pd

qb = QuantBook()
aapl = qb.add_equity("AAPL", Resolution.DAILY).symbol

history = qb.history(
    aapl,
    start=pd.Timestamp("2026-02-10"),
    end=pd.Timestamp("2026-03-12") + pd.Timedelta(days=1),
    resolution=Resolution.DAILY,
)
# history is a multi-index frame; flatten to the schema the fixture reader expects.
flat = history.reset_index()
flat = flat[["time", "open", "high", "low", "close", "volume"]]
flat["time"] = flat["time"].dt.strftime("%Y-%m-%d")
flat.to_csv("qc_price_history.csv", index=False)
print(f"wrote {len(flat)} daily bars")
```

Column ordering matters — must be exactly ``time,open,high,low,close,volume`` because ``FixtureDataReader`` reads by column name and the smoke test asserts the header.

## Step 4 — pull qc_equity.json (diagnostic)

```python
chart = api.read_backtest_chart(PROJECT_ID, BACKTEST_ID, name="Strategy Equity").chart
with open("qc_equity.json", "w") as f:
    f.write(json.dumps(chart.__dict__, default=str, indent=2))
```

Equity curve isn't asserted in Phase 3 — captured for diagnostic comparison only.

## Step 5 — write attribution.md

```markdown
# qc-aapl-phase3 fixture attribution

- Tutorial: https://www.quantconnect.com/docs/v2/writing-algorithms/importing-data/streaming-data/precomputed-ml-predictions
- Captured by: Tim
- Captured at (UTC ms): <fill in>
- QC project id: <fill in>
- QC backtest id: <fill in>
- Algorithm: PrecomputedMlPredictionsAapl (Phase 3 AAPL-only override of QC tutorial)
- Validation window: 2026-02-10 → 2026-03-12 (matches qc-precomputed-predictions fixture)
- Universe: AAPL (single-symbol)
- Brokerage model: <default — record from "Backtest Statistics" page>
- Commission model: <record exactly as shown in QC backtest report>
- Resolution: Daily
- Initial cash: $100,000
- Schedule: set_holdings @ 08:00 ET → fills at next session open (NEXT_BAR_OPEN equivalent)
```

## Step 6 — drop into repo

```bash
cd <repo>
mkdir -p PythonDataService/tests/fixtures/golden/qc-aapl-phase3
cp qc_orders.json qc_price_history.csv qc_equity.json \
   qc_algorithm_screenshot.png attribution.md \
   PythonDataService/tests/fixtures/golden/qc-aapl-phase3/
```

Now run the parity tests:

```bash
podman exec polygon-data-service python -m pytest \
  /app/tests/research/parity/test_qc_fixture_smoke.py -v
```

If smoke passes, implement ``_build_our_fills`` in ``test_qc_aapl_phase3_trade_parity.py`` (one engine-replay call + an adapter from ``LoggedTrade`` to ``OurFill``) and run the acceptance test:

```bash
podman exec polygon-data-service python -m pytest \
  /app/tests/research/parity/test_qc_aapl_phase3_trade_parity.py -v \
  --write-recon-report
```

If status is ``passed``, write the hand-authored summary to ``docs/references/reconciliations/qc-aapl-phase3.md`` and the Phase 3 acceptance gate is closed.
