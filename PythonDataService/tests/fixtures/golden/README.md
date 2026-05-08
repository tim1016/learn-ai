# Golden Fixtures

Deterministic (input, reference output, attribution) records used to prove
that a canonical implementation is numerically equivalent to an independent
reference. A port is not done until a golden fixture proves it.

## Three Evidence Layers

Every fixture must document all three layers:

| Layer | Source | What it proves |
|-------|--------|----------------|
| 1. Market input provenance | Polygon/Massive MCP, or synthetic | Where the input data came from |
| 2. Methodology provenance | BigData MCP, papers, official docs, `references/` | Why the formula is what it is |
| 3. Independent numerical oracle | py_vollib, QuantLib, LEAN, Cboe, hand-computed | What the expected output should be |

A fixture is **externally certified** only if Layer 3 is independent of the
canonical implementation. `reference_kind` encodes this.

## reference_kind Taxonomy

| Kind | Certified? | When to use |
|------|-----------|-------------|
| `external_reference` | ✅ | Oracle is a third-party library (py_vollib, QuantLib) |
| `cross_engine` | ✅ | Oracle is a second canonical engine in this repo (not the function under test) |
| `literature_formula` | ✅ | Oracle is derived directly from a textbook formula, independently evaluated |
| `hand_computed` | ✅ | Oracle is arithmetic on a tiny synthetic series, verifiable by inspection |
| `vendor_observed` | ❌ | Output observed from a vendor; methodology opaque |
| `internal_regression` | ❌ | Output is our own engine's historical output; detects drift, not equivalence |

## GPL Boundary

`py_vollib==1.0.1` is licensed under the GPL. It is used **only** in:
- `scripts/generate_fixtures.py` (fixture generation — never in production)
- `tests/` (validation — never imported at runtime)

py_vollib must never be imported by any file under `app/`. The canonical
implementation (`app/services/bs_greeks.py`) uses only scipy and the standard
library. See `requirements-light.txt` for the comment confirming this boundary.

## Directory Layout

```
tests/fixtures/golden/
├── manifest.json              # Fixture registry (source of truth)
├── manifest.schema.json       # JSON Schema (generated from Pydantic models)
├── README.md                  # This file
├── golden_support/            # Shared support library
│   ├── manifest.py            # Pydantic schema models
│   ├── conventions.py         # Pinned numerical units and constants
│   ├── hashing.py             # content_sha256 + file_sha256
│   ├── io.py                  # normalize_timestamp, Arrow IPC read/write
│   ├── compare.py             # Explicit-tolerance comparator
│   └── registry.py            # Fixture lookup by ID
└── options-pricing/
    └── BS-001/
        └── v1/
            ├── input.arrow
            ├── output.arrow
            └── attribution.md
```

Fixture files use Arrow IPC (`.arrow`) for numeric arrays and JSON for
metadata. Existing Parquet fixtures in this directory are left alone
(additive, not migrated).

## Adding a New Fixture

1. Assign an ID from the naming convention: `<CATEGORY>-<NNN>` (e.g. `BS-001`, `ENG-002`).
2. Run `scripts/generate_fixtures.py --id <ID> --justification "<reason>"`.
   The script creates `v1/` under the fixture's directory and adds a manifest entry with `status=planned`.
3. Review the generated `attribution.md`. Verify all three evidence layers are documented.
4. Change `status` in the manifest from `planned` to `active`.
5. Commit: the manifest entry, the Arrow files, and the attribution.
6. Add a validation test in `tests/fixtures/test_<category>_fixtures.py`.

## Overwriting a Fixture (--force)

The script refuses to overwrite an existing version without `--force`.
`--force` creates a new version directory (`v2/`, `v3/`, …) and does NOT
change `active_version` in the manifest. To activate the new version,
edit `manifest.json` and set `active_version` to the new number, then
commit with a message explaining why the fixture was regenerated.

**Never hand-edit fixture data files.** If the data is wrong, rerun the
reference and regenerate.

## Regeneration Workflow

Each `attribution.md` contains a `Regeneration:` section with the exact
command to reproduce the fixture. Before regenerating:

1. Confirm the reference version has not changed (check the oracle library version).
2. Run with `--force` to create a new version.
3. Diff the new output against the old. Expect only floating-point noise
   at the precision floor; larger changes indicate a real behavior change in
   the oracle or the canonical — investigate before activating.
4. Update `active_version` in the manifest only after confirming the change
   is intentional.

## Tolerance Philosophy

Every fixture declares `atol`, `rtol`, and a required `tolerance_note`:

- Default: `atol=1e-10, rtol=0` for cross-library BS comparisons.
- `1e-12` is excluded as a cross-library target. Linux x86_64 CI and Windows
  dev boxes diverge for transcendental functions at that level — use `1e-10`.
- Tolerances are never loosened to make a failing test pass. See
  `.claude/rules/numerical-rigor.md` → "Loosening tolerances" for the full
  classification procedure.

## CI

The `validate-golden-manifest` CI job runs `test_golden_manifest.py` on
every PR. It validates:
- manifest.json against manifest.schema.json
- All active fixture files exist on disk
- No duplicate IDs, no empty tolerance notes
- SHA-256 hashes are valid hex

Fixture validation tests (e.g. `test_options_pricing_fixtures.py`) run as
part of the standard `python-test` CI job.

Tolerance note: tolerances in fixture metadata are validated on **Linux x86_64
CI (ubuntu-latest, Python 3.12)**. Windows dev boxes are local environments,
not CI gates.

## Directories Outside Manifest Governance

Three directories in `tests/fixtures/golden/` predate the manifest system and are intentionally excluded from it. They are documented here so a reviewer does not mistake their absence from `manifest.json` for an oversight.

### `bs-price-cross-engine/`

**Kind:** live-parity test (no stored output)
**Test:** `tests/services/test_bs_cross_engine_parity.py`
**Purpose:** Pin equivalence between the two in-repo Black-Scholes engines (`bs_greeks.py::bs_european_price` and `quantlib_pricer.py::price_option`). Both engines compute at runtime on a shared input grid (`cases.json`). There is no stored `output.arrow` to store: the oracle is the second engine, not a precomputed number.

This does not fit the manifest's `FixtureFiles` schema (which requires a stored `output` file). It is governed entirely by its test file and is covered by Git history.

### `portfolio-scenario-3leg/`

**Kind:** live wiring test (no stored output)
**Test:** `tests/services/test_portfolio_scenario.py`
**Purpose:** Pin the integration behavior of `evaluate_scenario` against direct Hull-formula reference computation. Both the system-under-test and the reference compute at runtime using `cases.json` as the scenario grid.

Same reasoning as `bs-price-cross-engine/`: no stored output, so the manifest schema does not apply. Governed by its test file.

### `iv30/`

**Kind:** vendor market-data snapshot
**Test:** `tests/edge/test_iv30_and_vrp.py`, `tests/edge/test_iv30_stability.py`, `tests/routers/test_iv30_router.py`, `tests/volatility/test_vix_replication.py`
**Files:** `spy-2024-12-20-chain.parquet` (Polygon options chain), `spy-2024-12-20-chain.meta.json` (metadata + computed IV30 reference values)
**Purpose:** Hold a real Polygon SPY options chain snapshot (2024-12-20) used to validate the VIX-style IV30 computation against observed market data. The `.meta.json` captures the reference IV30 values at capture time.

This fixture is stored in Parquet format (not Arrow IPC) and lacks an `attribution.md`. See `attribution.md` in this directory for source and capture metadata. It is not registered in `manifest.json` because converting to Arrow IPC and adding SHA-256 governance is deferred (the file is small, stable, and covered by Git). If the parquet is regenerated, update `attribution.md` and the meta.json computed values.

## Links

- `docs/math-sources-of-truth.md` — concept-level canonical registry
- `docs/architecture/engine-authority-map.md` — engine-level ownership map
- `.claude/rules/numerical-rigor.md` — scientific standards
- `docs/references/golden-fixtures/` — per-fixture markdown docs
