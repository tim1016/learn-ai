# QC AAPL Phase 3 fixture-capture runbook

Hand-run in QC Cloud to produce ``tests/fixtures/golden/qc-aapl-phase3/``. After capture, drop the files into that directory and the two fixture-gated tests activate automatically.

> **Phase 3.5 update (2026-05-11):** the price-history pull is now
> ``Resolution.MINUTE`` (was ``Resolution.DAILY``) to support
> ``FillMode.NEXT_SESSION_OPEN`` parity. The QC algorithm itself stays
> on daily-bar universe + minute-equity (QC's brokerage simulates fills
> at minute granularity regardless). Multi-day fill count expected: 3
> on the 2026-02-10 → 2026-03-12 window (entry 02-10, exit 02-20, re-entry
> 02-21 — the only negative prediction in the set).

## Inputs you need before opening QC

- Validation window: **2026-02-10 → 2026-03-12** (must match PR #215's prediction-set window)
- Backtest cash: **$100,000**
- Symbol: **AAPL** (single-symbol degenerate of QC's full SP500 universe)
- Resolution: **MINUTE** for price-history pull (algorithm itself can keep DAILY for universe selection)

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

        # Phase 3 modification: AAPL-only universe. AAPL itself is added at
        # MINUTE resolution so QC's brokerage simulates fills at the 09:31
        # ET minute-bar boundary (matches Phase 3.5 NEXT_SESSION_OPEN
        # parity); universe selection stays daily (cheaper than
        # MINUTE-frequency universe re-selection).
        self.symbol_aapl = self.add_equity("AAPL", Resolution.MINUTE).symbol
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

## Step 2 — pull qc_orders.json (canonical schema required)

### Canonical fixture schema

The reconciler's parser is strict on purpose — it enforces a single canonical
schema and fails fast on any deviation. QC's raw API response carries
information (nested `Symbol` objects, `orderEvents` camelCase, numeric times in
seconds) that the runbook must normalize to the canonical shape **before** the
file is committed:

```json
{
  "orders": [
    {
      "id": 1,
      "symbol": "AAPL",
      "type": 0,
      "events": [
        {
          "time": "2026-02-11T13:30:00Z",
          "fillQuantity": 526,
          "fillPrice": 190.00,
          "direction": 0,
          "orderFeeAmount": 2.63
        }
      ]
    }
  ]
}
```

Schema rules (any violation makes `_parse_qc_orders` raise `FixtureSchemaError`):

1. Top-level object **must** have an `"orders"` key whose value is a JSON array.
2. Each order's `"symbol"` is a plain **string** (bare ticker or QC `"AAPL R735QTJ8XC9X"` with security-id suffix — the parser strips it). Nested `{"value": "AAPL"}` is rejected.
3. Each order's event list **must** be named `"events"` (not `"orderEvents"`).
4. Each event's `"time"` is either an ISO-8601 string with a `Z` or offset, **or** a numeric `int64` ms since epoch (`> 10**12`). Numeric seconds (`< 10**11`) are auto-promoted. Anything in between is rejected as ambiguous.
5. Each event has integer `"fillQuantity"`, numeric `"fillPrice"`, optional numeric/null `"orderFeeAmount"`.

### QC capture + normalization snippet

In a new QC Research notebook **bound to the same project as the algorithm**:

```python
from QuantConnect.Api import Api
import json

# Replace these — project id is in the project URL; backtest id is in the
# completed-backtest URL.
PROJECT_ID = 12345
BACKTEST_ID = "abcdef1234567890"

api = Api()
api.initialize(self.config.UserId, self.config.UserToken)

# Try the Python wrapper first; fall back to the HTTP endpoint if the
# wrapper isn't available on your QC client version.
try:
    raw_orders = api.read_backtest_orders(PROJECT_ID, BACKTEST_ID).orders
    raw_dicts = [o.__dict__ for o in raw_orders]
except Exception:
    # HTTP fallback — /backtests/orders/read is paginated; loop until done.
    # See https://www.quantconnect.com/docs/v2/cloud-platform/api-reference
    import requests
    raw_dicts = []
    start = 0
    while True:
        resp = requests.post(
            "https://www.quantconnect.com/api/v2/backtests/orders/read",
            auth=(api.UserId, api.UserToken),
            json={"projectId": PROJECT_ID, "backtestId": BACKTEST_ID, "start": start, "end": start + 100},
        ).json()
        page = resp.get("orders", [])
        if not page:
            break
        raw_dicts.extend(page)
        start += len(page)

# ── Normalization to canonical schema ─────────────────────────────────
def _flatten_symbol(s):
    if isinstance(s, str):
        return s
    # QC sometimes returns nested Symbol objects in the wrapper path.
    for attr in ("Value", "value", "ID", "id"):
        v = getattr(s, attr, None) if not isinstance(s, dict) else s.get(attr)
        if isinstance(v, str):
            return v
    raise ValueError(f"cannot flatten symbol: {s!r}")

def _normalize_event_keys(ev):
    # Accept camelCase or PascalCase; emit canonical camelCase.
    keymap = {
        "Time": "time", "time": "time",
        "FillQuantity": "fillQuantity", "fillQuantity": "fillQuantity", "fill_quantity": "fillQuantity",
        "FillPrice": "fillPrice", "fillPrice": "fillPrice", "fill_price": "fillPrice",
        "Direction": "direction", "direction": "direction",
        "OrderFeeAmount": "orderFeeAmount", "orderFeeAmount": "orderFeeAmount", "order_fee_amount": "orderFeeAmount",
    }
    return {keymap[k]: v for k, v in ev.items() if k in keymap}

normalized = []
for o in raw_dicts:
    # Event list lives under either "Events", "events", or "orderEvents".
    raw_events = o.get("Events") or o.get("events") or o.get("orderEvents") or []
    normalized.append({
        "id": int(o.get("Id") or o.get("id")),
        "symbol": _flatten_symbol(o.get("Symbol") or o.get("symbol")),
        "type": int(o.get("Type") or o.get("type") or 0),
        "events": [_normalize_event_keys(e) for e in raw_events],
    })

with open("qc_orders.json", "w") as f:
    json.dump({"orders": normalized}, f, indent=2, default=str)
print(f"wrote {len(normalized)} orders in canonical schema")
```

## Step 3 — pull qc_price_history.csv (MINUTE)

Same notebook:

```python
import pandas as pd

qb = QuantBook()
aapl = qb.add_equity("AAPL", Resolution.MINUTE).symbol

history = qb.history(
    aapl,
    start=pd.Timestamp("2026-02-10"),
    end=pd.Timestamp("2026-03-12") + pd.Timedelta(days=1),
    resolution=Resolution.MINUTE,
)
# history is a multi-index frame; flatten to the schema the fixture reader expects.
flat = history.reset_index()
flat = flat[["time", "open", "high", "low", "close", "volume"]]
# Minute-resolution timestamps include HH:MM:SS (NY-local wall-clock per QC's
# qb.history convention for US equities). The fixture reader auto-detects
# minute vs daily resolution from the time column's time-of-day component.
flat["time"] = flat["time"].dt.strftime("%Y-%m-%d %H:%M:%S")
flat.to_csv("qc_price_history.csv", index=False)
print(f"wrote {len(flat)} minute bars")

# Sanity-check the boundaries — must match what the Phase 3.5 smoke test asserts.
assert flat["time"].iloc[0] == "2026-02-10 09:30:00", flat["time"].iloc[0]
assert flat["time"].iloc[-1].startswith("2026-03-12 15:5"), flat["time"].iloc[-1]
```

Column ordering matters — must be exactly ``time,open,high,low,close,volume`` because ``FixtureDataReader`` reads by column name and the smoke test asserts the header.

Expected row count: ~8,000 (≈22 trading days × 390 minutes/session). Significantly larger than the prior daily fixture's 22 rows — the smoke test pins the first and last timestamps explicitly.

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
- Resolution: Minute (price history) / Daily (universe selection)
- Initial cash: $100,000
- Schedule: set_holdings @ 08:00 ET → fills at next minute open (09:31 ET first-minute bar; matches Phase 3.5 NEXT_SESSION_OPEN parity)
- Expected fills (Phase 3.5 multi-day window): 3 — entry 02-10, exit 02-20, re-entry 02-21 (only negative prediction in the set is 2026-02-20)
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
