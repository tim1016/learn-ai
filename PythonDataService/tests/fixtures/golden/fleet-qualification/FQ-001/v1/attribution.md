# Qualification recorded-history provider — determinism fixture

**Registered in `manifest.json` as `FQ-001`, `reference_kind: internal_regression`.**
`app.services.broker_v2_panel.qualification_recorded_history` is a
**synthetic, seeded generator** built for issue #2206 (qualification-only
recorded history provider) — it does not claim to reproduce real Polygon
prices, only to be *deterministic* given `(symbol, bar_start_ms)`. This
fixture pins that determinism (a regression pin against our own prior
output, `internal_regression` in the golden-fixture taxonomy — see
`tests/fixtures/golden/README.md`), not equivalence to a vendor.

## What this proves

`output.json` is the exact `HistoryBatchResponse` the generator produces for
one fixed request (`input.json`).
`tests/broker/v2panel/test_qualification_recorded_history.py::test_golden_batch_matches_the_committed_fixture`
loads it and asserts the live function still reproduces it byte-for-byte.
If that test ever fails, either the generator changed behavior (regenerate
and explain why, per the numerical-rigor regeneration discipline) or a
real bug was introduced.

**Bit-exact on every platform.** Every price is built from integer cents
(a triangle wave and a SHA-256-derived offset, both pure integer
arithmetic) and converted to a float exactly once, by dividing by 100 --
IEEE-754 division is correctly rounded on every conformant platform, unlike
`math.sin`. An earlier version of this generator used `math.sin` for the
wave and 814 of 20,000 sampled instants differed by 1 ulp between macOS
arm64 and Linux x86_64, which broke this fixture's exact-string comparison
on CI (Linux x86_64) even though it passed locally on the machine that
generated it. This fixture was regenerated after that fix.

## Input

```json
{"symbol": "QUALHIST", "timeframe": "1d", "required_bar_count": 5, "as_of_ms": 1700000000000}
```

## Methodology (Layer 2 — no external reference; the algorithm is the source)

Documented in full in the generator's own module docstring
(`app/services/broker_v2_panel/qualification_recorded_history.py`). Summary:

1. Daily bars are stamped at 00:00 America/New_York of the real NYSE session
   date (Polygon's own daily-bar convention, confirmed by this repo's
   `test_daily_bar_completes_at_session_close_not_midnight_plus_one_day`),
   derived from the canonical calendar module
   (`app.lean_sidecar.trading_calendar.session_windows_ms_utc`) — never a
   hardcoded session time.
2. Each bar's `close` is `_BASE_PRICE_CENTS + wave_cents(t_ms) + noise_cents(symbol, t_ms)`,
   in integer cents. `wave_cents` is a triangle wave (pure integer modulo
   and floor division); `noise_cents` reduces a SHA-256 hash of
   `f"{symbol}:{t_ms}:{salt}"` with integer `%` — deterministic and
   independent of process, run order, platform, or `PYTHONHASHSEED` (unlike
   Python's builtin `hash()`).
3. `open` is the previous slot's `close`, computed directly (not cached) --
   determinism, not a gap-free continuous series (see the generator's own
   `_deterministic_bar` docstring for why those are different claims).
4. `high`/`low`/`volume` are derived from further salted hashes of the same
   `(symbol, t_ms)`, so the OHLC invariants (`high >= max(open, close)`,
   `low <= min(open, close)`) hold by construction.
5. Every price is divided by 100 (integer cents -> float dollars) exactly
   once, at the very end.

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
" > tests/fixtures/golden/fleet-qualification/FQ-001/v1/output.json
```

Regenerate only if the generator's algorithm intentionally changes (never to
make a failing test pass — same rule as every other golden fixture in this
repo). After regenerating, update `manifest.json`'s `content_sha256`/
`file_sha256` entries for `FQ-001` (see `golden_support/hashing.py`).

Generated 2026-09-18 against `qualification_recorded_history.py`; regenerated
the same day after replacing `math.sin` with integer-cents arithmetic for
cross-platform bit-exactness (see "Bit-exact on every platform" above) and
after correcting daily bars to Polygon's midnight-ET stamp.
