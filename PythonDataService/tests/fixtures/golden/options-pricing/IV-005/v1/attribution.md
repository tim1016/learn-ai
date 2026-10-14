# IV-005 — SPY Options Chain Snapshot (2024-12-20) and its IV30

**Registered in `manifest.json` as `IV-005`, `reference_kind: internal_regression`.**
The chain is real Polygon market data, frozen. The reference IV30 in the
sidecar was written by our own `vix_style_iv30` when the snapshot was built.
This fixture detects *drift* in the VIX-style replication on a real chain; it
is **not** an independent oracle and does not prove equivalence to CBOE (see
"What this proves and what it does not"). The independent CBOE-formula
oracles are IV-003 and RV-004, on synthetic chains.

## Evidence Layers

**Layer 1 — Market input provenance:** Polygon.io, SPY, as of 2024-12-20.
`/v3/reference/options/contracts` (strike, expiry, type) and
`/v2/aggs/ticker/{ticker}/range/1/day/{from}/{to}` (daily close). Daily
aggregates carry no bid/ask, so every leg's spread is synthesized from the
close (see Assumptions).

**Layer 2 — Methodology provenance:** CBOE VIX 2019 whitepaper "VIX Index
Calculation: Step-by-Step"; Demeterfi, Derman, Kamal, Zou (1999) Goldman Sachs
Quantitative Strategies Research Notes. The parametric number in the sidecar
uses an ATM variance-time interpolation (`iv30_atm_50d`).

**Layer 3 — Independent numerical oracle:** none. `vix_style_iv30_act365` in
the sidecar is the output of `app/volatility/vix_replication.py::vix_style_iv30`
on this chain, written by `scripts/build_iv30_golden.py`.

## Files

| File | Role | Description |
|------|------|-------------|
| `spy-2024-12-20-chain.parquet` | input | 881 contracts: `ticker`, `strike`, `expiration_date`, `expiry_days`, `contract_type`, `close` |
| `spy-2024-12-20-chain.meta.json` | output | Spot, rate, dividend, straddling expiries and the reference IV30 values |
| `attribution.md` | — | This file |

## Reference values (`spy-2024-12-20-chain.meta.json`)

| Field | Value |
|-------|-------|
| Spot | 591.15 |
| Risk-free rate | 0.04240 (FRED) |
| Dividend yield | 0.01195 (Polygon TTM) |
| `vix_style_iv30_act365` | 0.17305460843066506 |
| `parametric_iv30` (ATM, per-expiry solve) | 0.15583619498680715 |
| Straddling expiries | 28 d and 35 d |
| Contracts | 881 |

## What this proves and what it does not

- `tests/volatility/test_vix_replication.py::TestSpyGoldenFixture::test_replication_matches_meta_sidecar_deterministically`
  recomputes `vix_style_iv30` from the frozen chain and asserts it equals the
  sidecar value to 1e-9. The sidecar was written by the same function, so this
  is a regression pin: it fails if the replication drifts, it cannot say the
  replication is right.
- `TestSpyGoldenFixtureWithProvenance::test_replication_with_provenance_matches_meta_sidecar`
  asserts the production path (`vix_style_iv30_with_provenance`, normalized
  legs) reproduces the same value to 1e-9. Two code paths agreeing is
  evidence of path parity, not of CBOE equivalence.
- `test_vix_replication_in_realistic_band` and `test_skew_premium_below_300bps`
  read only the sidecar: they bound the stored numbers (13-22 % and < 300 bps),
  they do not run the replication.
- The test docstring cites the published VIX index at about 17.5 % for that
  day; that figure is not verified in this repository, and the band above is a
  sanity check, not a CBOE comparison.

## Assumptions

- **Half-spread:** `max($0.05, 0.005 * max(call close, put close, 1.0))` per
  strike, shared by both legs; a contract with close below $0.05 is treated as
  zero-bid (the sidecar's `half_spread_policy` string abbreviates this as
  "0.5% of close"). The test rebuilds the quotes from `close` with the same rule.
- **Rate:** FRED-derived, one rate for both expiries. **Dividend:** Polygon
  trailing twelve months; used by the parametric number only.
- **Bar timing:** daily close as of 2024-12-20; no intraday NBBO. Expiries are
  calendar days from the as-of date (`expiry_days`).
- **Why this date:** a standard expiry calendar (no early close or holiday), a
  moderate IV regime (about 17 % IV30), complete Polygon data for SPY, and two
  expiries (28 d, 35 d) that bracket the 30-day interpolation point.

## Tolerance

`atol=1e-9, rtol=0`. The comparison is the same function evaluated again on
the same bytes, so the strict-float default applies and nothing looser is
needed.

## Regeneration

```bash
podman exec polygon-data-service python /app/scripts/build_iv30_golden.py
```

The script needs a Polygon key and rewrites the parquet and the sidecar in
this directory. A re-capture is not byte-reproducible (Polygon may revise
historical aggregates): treat it as a new version, check that
`vix_style_iv30_act365` stays within ±50 bps of 0.17305, then register it as
`v2` in `manifest.json` with a justification and update the reference values
above. Never edit these files by hand.

## Generation Metadata

Generated: 2026-04-26 (commit `a32836a6`, as-of date 2024-12-20)
Oracle: internal_regression — our own `vix_style_iv30` output, self-referential
Script: `scripts/build_iv30_golden.py`
Registered in the manifest: #3221 (the bytes were not regenerated; the files
moved from `golden/iv30/` to this directory unchanged)
