---
id: F-0026
severity: P2
status: partially_resolved
area: fixture
canonical_file: PythonDataService/tests/fixtures/golden/
reference: .claude/rules/numerical-rigor.md (Golden fixtures); docs/math-sources-of-truth.md
first_seen: 2026-05-06
last_seen: 2026-05-08
phase: 5
---

## What

**Original finding (2026-05-06):** The golden fixture directory contained only 3 directories. The overwhelming majority of canonical math had no golden fixture under `tests/fixtures/golden/`.

**Current state (2026-05-08):** The manifest now governs **31 active fixtures** across 7 categories (`options-pricing`, `engine-statistics`, `indicators`, `realized-volatility`, `options-pricing`, `research-primitives`, `indicator-reliability`). Programmatic parity tests (`test_indicator_parity.py`) remain outside the golden-fixture system, but the fixture catalog is substantially built out.

## Remaining gaps

### 1. Three legacy directories are outside manifest governance

```
tests/fixtures/golden/bs-price-cross-engine/   { attribution.md, cases.json }
tests/fixtures/golden/portfolio-scenario-3leg/ { attribution.md, cases.json }
tests/fixtures/golden/iv30/                    { spy-2024-12-20-chain.meta.json, *.parquet }
```

These predate the manifest system and have never been registered. The `manifest.json` API reads only `manifest.json`; these directories are invisible to the catalog and the UI.

- `bs-price-cross-engine` and `portfolio-scenario-3leg` are live-parity fixtures with no stored output — both engines compute at runtime. They do not fit the golden-fixture schema (which requires stored `output.arrow`). These should be explicitly documented in `README.md` as "live-parity directories" and excluded from manifest governance by design.
- `iv30/` holds a real Polygon market-data parquet snapshot used by `test_vix_replication.py`. It is missing an `attribution.md`. It could either be registered in the manifest (if converted to Arrow format) or documented in `README.md` as a "vendor market-data fixture" outside manifest governance.

### 2. Hash governance does not cover attribution files

The `content_sha256` and `file_sha256` dicts in each manifest entry cover `input.arrow` and `output.arrow` but not `attribution.md`. An attribution file can be silently modified without the manifest detecting it. No test catches this.

No test in `test_golden_manifest.py` recomputes file hashes and compares them against the manifest — the existing hash validation (`test_hash_fields_are_valid_hex`) only checks that stored values are valid hex strings. A modified fixture file would not be caught by CI.

### 3. Programmatic parity tests remain outside the golden-fixture pattern

`test_indicator_parity.py` asserts parity programmatically (no stored reference output). Per `.claude/rules/numerical-rigor.md`, full equivalence proof requires a stored golden fixture with attributed reference output. These indicators (SMA, EMA, RSI, MACD, etc.) remain under `pending-fixture` debt. Burn down on touch per the legacy-debt rule.

### 4. Remaining `pending-fixture` rows in `docs/math-sources-of-truth.md`

Several canonical math concepts still lack any fixture or parity test:
- Bar consolidation, event replay, fill models
- Dividend adjustment (reference: "CRSP placeholder")
- Strategy-level golden fixtures (SPY EMA Crossover, ORB, etc.)

## Why this severity

Downgraded to P2 (was P1) because:
- The manifest system is now built and governs 31 active fixtures
- The remaining gaps are documentation/governance gaps, not a missing fixture system
- The programmatic parity tests likely cover the math correctly even without stored fixtures
- The hash-recompute gap is a new sub-finding that can be addressed independently

## Suggested resolution

1. **Immediate:** Document `bs-price-cross-engine` and `portfolio-scenario-3leg` in `README.md` as live-parity directories excluded from manifest governance by design. Add `attribution.md` to `iv30/` and document it similarly.
2. **Immediate:** Add a `test_file_hashes_match_disk()` test to `test_golden_manifest.py` that recomputes `file_sha256` for each active fixture and compares against the manifest. Add a parallel `test_content_hashes_match_disk()` test.
3. **Immediate:** Add a test that validates attribution file hashes when present in the manifest (forward-looking enforcement for new fixtures).
4. **Burn-down:** Add golden fixtures for indicator parity on touch. Do not batch-create 30 fixtures in one PR.
5. **Burn-down:** Address each `pending-fixture` registry row as the corresponding canonical is touched.

## Provenance of the finding itself

Phase 5 / cursor: `Glob("PythonDataService/tests/fixtures/golden/**/*")` returned 6 paths in 3 directories (2026-05-06). Updated 2026-05-08 after manifest system was built out to 31 active fixtures.
