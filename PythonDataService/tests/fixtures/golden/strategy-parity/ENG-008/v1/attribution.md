# ENG-008 — Strategy A/B/C self-equivalence (pre-port receipt)

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. A single seeded random-walk series of 900 15-minute SPY-like bars (`numpy.random.default_rng(seed=20260820)`, base price $400, per-bar step `N(0, 1.35)`), shared across all three strategies. See `scripts/fixture_generators/strategy_abc_self_equivalence.py::build_bars`.

**Layer 2 — Methodology provenance:** None external — this fixture exists to prove *refactor neutrality*, not mathematical correctness. Strategy A/B/C (`app/engine/strategy/algorithms/spy_strategy_{a,b,c}.py`, sharing `_rsi_range_base.py::RsiRangeStrategy`) were never ported from an outside reference (see each file's own provenance block).

**Layer 3 — Independent numerical oracle:** None — `reference_kind=internal_regression`. The oracle is Strategy A/B/C's own current trade_log output, captured once at their registered default parameters. It certifies nothing about correctness; it exists so GitHub issue #1699's S3 intent port can prove numerical neutrality against it rather than argue it.

## Protocol

Each strategy is constructed with zero constructor overrides (registered defaults: `app/engine/strategy/registry.py` `spy_strategy_{a,b,c}` entries), run once through `BacktestEngine` against the shared bar series (`FillModel(mode=SIGNAL_BAR_CLOSE, commission_per_order=0)`), and its public `strategy.trade_log` (`list[LoggedTrade]`) is captured verbatim — not the private `_entry_extra_gate_passes` gate the existing `app/engine/tests/test_strategies_abc.py` drives. Trade counts at generation: strategy_a=37, strategy_b=11, strategy_c=7.

## Tolerance

`atol=0, rtol=0` (bit-exact). Every `LoggedTrade` field is produced by exact Decimal arithmetic (subtraction and division under a fixed `decimal` context, no float path) replayed against the same pinned Arrow input — regeneration with the recorded command reproduces the fixture exactly. See `.claude/rules/numerical-rigor.md` "Equivalence levels" — this is a Bit-exact receipt, not Strict float.

## Regeneration

  PYTHONPATH=. python scripts/fixture_generators/strategy_abc_self_equivalence.py

Run from the `PythonDataService` directory. Rerun only when Strategy A/B/C's math intentionally changes (e.g. after the S3 intent port lands, to re-baseline); a change in this fixture's committed trades is itself the numerical-neutrality verdict for that port and must be justified in the commit message, per the golden-fixture lifecycle rules.

## Generation Metadata

Generated: 2026-08-20
Oracle: internal_regression — Strategy A/B/C's own current trade_log output
Script: scripts/fixture_generators/strategy_abc_self_equivalence.py
