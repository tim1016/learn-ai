# Walk-forward analysis

**Concept**: Split a date window into train/test folds and aggregate fold-level metrics into a single out-of-sample (OOS) view. A fixed-spec run replays one `StrategySpec` on every test window. A parameter-search run executes fully materialized candidates on train, freezes one deterministic winner, and evaluates only that winner on test. Both paths use the canonical engine.

**Reference**: López de Prado, *Advances in Financial Machine Learning* (2018), §7 — "Cross-Validation in Finance" — establishes walk-forward as the standard CV protocol for time-series strategies, where standard k-fold is invalid because of look-ahead leakage. **Verify on next touch** — the citation is approximate; §7 covers walk-forward conceptually but the specific split-policy taxonomy (chronological / rolling / anchored) is repository-internal.

**Canonical implementation**: `PythonDataService/app/research/walk_forward/` + `app/routers/walk_forward.py`. The aggregation metrics, storage, failure semantics and HTTP boundary are documented there.

The split policies, half-open fold boundaries, train-window pre-roll, flat positions at test boundaries, train-side selection and the compounded (not rebased) combined OOS curve are decisions in [ADR 0073](../architecture/adrs/0073-research-verdict-rules.md) decision 3.
