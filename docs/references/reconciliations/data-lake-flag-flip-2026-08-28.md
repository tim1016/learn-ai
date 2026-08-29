# Data lake vs policy store — the flag-flip parity receipt

**Date:** 2026-08-28
**Slice:** #1839 (data-lake enablement, PRD #1825). Decision record: ADR 0049.

**Purpose:** `DATA_LAKE_ENABLED` defaults on from this slice, which makes the data lake the
market-data authority for raw historical bars. This receipt records how the replacement was
proven to be an identity rather than an approximation, at what equivalence level, and what was
deliberately *not* claimed.

## What was reconciled

Two paths to the same bars:

- **Pre-import (the old path):** a policy-keyed cache under
  `PythonDataService/app/engine/data/policy_store.py`, written by
  `app/engine/data/lean_format.py::write_lean_day_zip`.
- **Post-import (the new path):** the lake under `app/data_lake/`, populated from that cache by
  `app/data_lake/cache_import.py::import_cache_root`.

Consumers on both sides: the Python engine's `LeanMinuteDataReader`, the LEAN sidecar's
read-only lake mount, and the chart split-read.

## Equivalence levels — three, not one

`.claude/rules/numerical-rigor.md` asks that a port state its level. Three levels apply here
and collapsing them would overstate two.

| Claim | Level | Tolerance |
|---|---|---|
| Imported lake artifact vs its source cache zip | Bit-exact | byte equality; SHA-256 equality |
| `data_availability_hash`, lake-read vs cache-read | Bit-exact | hash equality |
| Decoded bar streams, lake vs cache | Bit-exact | exact `Decimal` equality (`atol=0`, `rtol=0`) |
| Lake writer vs policy-store writer, same input bars | **Row-exact, NOT byte-exact** | exact equality per CSV row |

**Why bit-exact is available at all.** `cache_import` promotes `verified.raw_bytes` — it does
not re-encode — so an imported artifact and its source zip are the same file under two names.
Everything downstream inherits that: the SHA the catalog records, the fingerprint the run
receipt carries, the bytes LEAN reads off the mount.

**Why no `atol` on the bars.** Both readers decode the same integers off LEAN's 1/10,000
deci-cent grid into `Decimal`. There is no floating-point step between the bytes and the values,
so a tolerance would admit a difference that cannot arise and conceal one that could.

**What is not claimed.** The two *writers* are row-identical but not byte-identical: the lake
writer terminates its CSV with a newline and the policy writer does not. One byte differs and no
reader can observe it. Byte-equality between the two writers is claimed nowhere in this repo, and
both halves of that weaker claim — the row equality and the byte inequality — are asserted in
`tests/unit/data_lake/test_deci_cent_canonical.py::test_both_writers_encode_identically_on_sub_grid_prices`,
which is the only place that builds the two zips from the *same bars* and can therefore assert
the inequality for its real reason. (An earlier draft asserted it in the parity suite against a
five-bar zip and a 390-bar imported day, where the row counts alone drive the difference; that
assertion could not have failed for its stated reason and was removed.) Mutation-checked: giving
the policy writer a trailing newline fails the guard.

## Prerequisite fixed before the comparison was trusted

The two writers disagreed on rounding. `lean_format` truncated (`int(price * 10_000)`); the lake
writer rounded half-up. On a price finer than the grid — a sub-$1.00 Reg NMS tick, or a provider
revision carrying more precision than the tape — they differed by one deci-cent.

**Half-up is canonical** (`app/data_lake/lean_writer.py::to_deci_cent`, now called by every
writer in the tree). Encoding onto a fixed grid is a quantization, so the target is the nearest
representable value; truncation is not a rounding rule but a systematic bias, with expected error
−0.5 deci-cents on every OHLC field of every bar against half-up's 0. A bias that never changes
sign accumulates through a strategy.

Registered in `docs/math-sources-of-truth.md`.

## Divergences

**Zero, at every level claimed.** No entry in the `reconcile-backtest` taxonomy was exercised,
because no divergence survived to be classified. The one divergence present at the start of the
slice (the writer rounding above) was eliminated rather than tolerated, which is what
numerical-rigor's loosening rule requires.

One **known, accepted, non-numerical input divergence** is recorded separately and is not a bar
difference: a lake-mode LEAN sidecar run has no `alternative/interest-rate` subtree where a
staging-mode run does, so LEAN falls back to its built-in risk-free rate. These are equity-only
backtests with no option pricing, so that rate feeds portfolio *statistics* and never a fill, a
commission, or a position size; those statistics leave the sidecar as strings
(`normalized_parser` keeps `statistics` as `dict[str, str]`) and no category in this taxonomy
gates on them. It is not fixable from inside the data plane (the launcher's `/extract-metadata`
contract returns two byte fields), is logged once per run, and is documented in
`app/lean_sidecar/lake_mount.py`'s module docstring.

## Tests

`PythonDataService/tests/integration/data_lake/test_flag_flip_parity.py` — 8 tests.

Seven need no Postgres and therefore run in CI, where the "Python Tests" job sets no
`POSTGRES_URL`. They exercise the byte path: the importer's own verify/promote primitives, the
readers, and the fingerprint function. The eighth is gated and documents why it must be — "zero
provider calls" is a statement about what `ensure_data` decides *after* consulting the catalog,
and a fake catalog would be making that decision for us.

Supporting: `tests/unit/data_lake/test_deci_cent_canonical.py` (the canonical rounding rule, and
the two writers' row-level agreement on sub-grid prices).

**Mutation-checked:** replacing the importer's verbatim byte copy with a re-encode through the
lake writer fails exactly the two byte-level assertions and leaves the row-level one green.

## Execution

- Without Postgres: 7 passed, 1 skipped.
- Against an ephemeral Postgres carrying the dev schema: 8 passed, 0 skipped. The whole gated
  lake surface (`tests/integration/data_lake` + `tests/unit/data_lake`) ran live in the same
  configuration: 318 passed, 0 skipped.
