# Qualification recorded-history provider — determinism fixture

**Not a numerical-equivalence port.** This directory is intentionally
outside `tests/fixtures/golden/manifest.json`'s governance, for the same
reason `bs-price-cross-engine/` and `portfolio-scenario-3leg/` are (see the
parent `README.md`'s "Directories Outside Manifest Governance" section):
there is no independent oracle here. `app.services.broker_v2_panel.qualification_recorded_history`
is a **synthetic, seeded generator** built for issue #2206 (qualification-only
recorded history provider) — it does not claim to reproduce real Polygon
prices, only to be *deterministic* given `(symbol, bar_start_ms)`. This
fixture pins that determinism, not equivalence to a vendor.

## What this proves

`output.json` is the exact `HistoryBatchResponse` the generator produces for
one fixed request. `tests/broker/v2panel/test_qualification_recorded_history.py::test_golden_batch_matches_the_committed_fixture`
loads it and asserts the live function still reproduces it byte-for-byte.
If that test ever fails, either the generator changed behavior (regenerate
and explain why, per the numerical-rigor regeneration discipline) or a
real bug was introduced.

## Input

```json
{"symbol": "QUALHIST", "timeframe": "1d", "required_bar_count": 5, "as_of_ms": 1700000000000}
```

## Methodology (Layer 2 — no external reference; the algorithm is the source)

Documented in full in the generator's own module docstring
(`app/services/broker_v2_panel/qualification_recorded_history.py`). Summary:

1. Every bar-start timestamp comes from the canonical NYSE calendar module
   (`app.lean_sidecar.trading_calendar.session_windows_ms_utc`) — never a
   hardcoded session time.
2. Each bar's `close` is `_BASE_PRICE + wave(t_ms) + noise(symbol, t_ms)`,
   where `wave` is a fixed-period sine and `noise` is a SHA-256 hash of
   `f"{symbol}:{t_ms}:{salt}"` normalized to `[0, 1)` — deterministic and
   independent of process, run order, or `PYTHONHASHSEED` (unlike Python's
   builtin `hash()`).
3. `open` is the previous slot's `close`, computed directly (not cached), so
   the series is continuous even across the backward-widening walk's
   disjoint fetch windows.
4. `high`/`low`/`volume` are derived from further salted hashes of the same
   `(symbol, t_ms)`, so the OHLC invariants (`high >= max(open, close)`,
   `low <= min(open, close)`) hold by construction.

## Regeneration

```bash
cd PythonDataService
POLYGON_API_KEY=test-key .venv/bin/python -c "
import asyncio, json
from app.services.broker_v2_panel.qualification_recorded_history import build_qualification_recorded_history_batch

async def main():
    batch = await build_qualification_recorded_history_batch(
        symbol='QUALHIST', timeframe='1d', required_bar_count=5, as_of_ms=1700000000000
    )
    print(json.dumps(batch.model_dump(mode='json'), indent=2, sort_keys=True))

asyncio.run(main())
" > tests/fixtures/golden/qualification-recorded-history/output.json
```

Regenerate only if the generator's algorithm intentionally changes (never to
make a failing test pass — same rule as every other golden fixture in this
repo). Generated 2026-09-18 against this PR's `qualification_recorded_history.py`.
