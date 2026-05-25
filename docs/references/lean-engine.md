# LEAN Engine — vendored reference (extract)

**Source**: QuantConnect LEAN Algorithmic Trading Engine.  
**Vendored commit**: `7986ed0aade3ae5de06121682409f05984e32ff7` (master HEAD as of 2026-04-26).  
**Vendored path**: `references/lean/7986ed0aade3ae5de06121682409f05984e32ff7/`  
**Attribution manifest**: `references/lean/7986ed0aade3ae5de06121682409f05984e32ff7/attribution.md`

## What was ported

| learn-ai file | LEAN source | Notes |
|---|---|---|
| `app/engine/consolidators/trade_bar_consolidator.py` | `LEAN/Engine/DataFeeds/Consolidators/TradeBarConsolidator.cs` | Bar consolidation logic — emits an OHLCV bar when the time window closes |
| `app/engine/execution/fill_model.py` | `LEAN/Engine/Execution/ImmediateFillModel.cs` / `Common/Orders/Fills/EquityFillModel.cs` | Immediate fill at bar close for on-time consolidated bars; opt-in LEAN equity stale-signal path fills at the current minute open after a session/data gap. No partial-fill or market-impact simulation |
| `app/engine/execution/intrabar_resolver.py` | LEAN execution semantics (no single file) | Resolves whether an order can be filled within the current bar |
| `app/engine/execution/portfolio.py` | `LEAN/Algorithm/QCAlgorithm.Portfolio` | Position tracking — cash, equity, unrealized PnL |
| `app/engine/execution/order.py` | `LEAN/Orders/Order.cs` | Order lifecycle states |
| `app/engine/execution/execution_config.py` | LEAN `QCAlgorithm` config pattern | Fill and commission config |
| `app/engine/engine.py` (orchestration) | `LEAN/Engine/` (top-level event loop) | Bar-by-bar event dispatch; indicators updated before `OnData` fires |

## Indicators ported from LEAN

Ported indicators with LEAN parity tests are individually cited in `docs/references/{rsi,sma,ema,macd,...}.md`. The LEAN vendored path serves as a single ground-truth for all of them.

## What was NOT ported

The following LEAN subsystems are absent or intentionally different in learn-ai:

- **Universe selection** — learn-ai uses a fixed symbol list per run.
- **Data feeds** — Polygon.io is the only data source; no LEAN live feed adapters.
- **Brokerage models** — commission and slippage are configurable but simplified vs. LEAN's brokerage library.
- **Risk management** — no LEAN risk handler; position sizing is strategy-specific.
- **Live trading** — learn-ai is research-only; no LEAN live algorithm runner.

## Pinning policy

The vendored extract is pinned to commit `7986ed0aade3ae5de06121682409f05984e32ff7`. If LEAN's upstream reference implementation changes, our port does NOT change automatically. Upgrades are:

1. Deliberate (triggered by a specific reconciliation need or a bug found in the reference).
2. Tested via the golden fixture suite before merging.
3. Documented in a commit message citing the old and new commit SHAs.

## Registry rows that cite this reference

- Bar consolidation, event replay, fill models (`app/engine/` subtree)
- SPY EMA Crossover (via LEAN indicator semantics + TV parity)
- RSI Mean Reversion (via LEAN indicator semantics)
- SMA Crossover (via LEAN indicator semantics)
- All ported indicators (EMA, RSI, SMA, MACD, ADX, ATR, Bollinger Bands, etc.)
