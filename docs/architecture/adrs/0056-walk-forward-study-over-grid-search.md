# ADR 0056 — A walk-forward study is folds of Grid Search sweeps with a frozen verdict; the SPY EMA protocol and Exhaustive Run retire

**Status:** Accepted 2026-09-05
**Provenance:** PRD [#1925](https://github.com/tim1016/learn-ai/issues/1925) revision 7 and its adversarial review; built in the same AFK session as PRD #1926 (Grid Search, ADR 0055). Decisions taken where the PRD was silent are tabled in `docs/references/walk-forward-study.md` for the operator's review.
**Decision drivers:** Walk-forward's selection step *is* a grid search over a training window. Two sweep implementations would mean two receipts, two warmup policies, two cancellation contracts and two Finish semantics for the same backtests. The existing SPY EMA normalized-gap protocol and its Exhaustive Run were frozen to one strategy, one instrument and one 18-fold window; a generic procedure makes them redundant.
**Related:** ADR 0055 (Python-owned research tables; `owner_kind` was reserved for exactly this), ADR 0022 (`int64 ms UTC`; the calendar is the session authority), `docs/references/grid-search.md`.

## Decision

1. **The study is a procedure, not an engine.** `app/research/walk_forward_study/service.py` drives Grid Search through its callable interface (`prepare_launch` → `create` → `execute`) once per fold and window. Per-fold sweeps are ordinary `research_grid_searches` rows with `owner_kind = 'walk_forward'`, `owner_id`, `fold_index` and `phase`; they never appear in Grid Search history and are deleted with the study.
2. **Selection uses the ranking contract; evidence is one cell per fold.** The training leader is the fold winner. The test sweep runs the whole grid (the same warmup policy, the same receipt) and every cell but the winner's is marked `exploratory`.
3. **The study freezes once.** One data snapshot over the whole range (run-up sessions included) and one code identity at launch; each fold sweep is launched with that snapshot and binds its reads to it, so no fold can run on other bytes.
4. **Durable record with the same fence.** `research_walk_forward_studies` (schema v2) holds the request, the receipt, the folds as JSON updated after every step (sweep ids before a cell runs, so Finish reuses them), and the verdict; `attempt` is claimed atomically and every write checks it.
5. **The verdict is frozen code, not configuration.** `verdict.py` implements the PRD's rule table over fold-winner retention (median; coverage floor `ceil(S/2)`; threshold 0.5; Sharpe regardless of ranking measure) and always discloses "based on D of S folds".
6. **Retirement.** Job types `spy_ema_walk_forward` and `spy_ema_exhaustive`, `app/research/walk_forward/spy_ema.py`, `app/research/exhaustive_run/`, their routers, schemas, tests, frontend pages, nav entries and user manual are removed. `docs/references/spy-ema-normalized-gap-walk-forward.md` stays for its formula provenance. Persisted artifacts on disk are not deleted by this decision.

## Consequences

- A change to how a sweep runs (warmup, cancellation, snapshot binding, ranking) changes walk-forward for free, and can break it for free; `tests/research/walk_forward_study/test_service.py` pins the contract the study relies on.
- Overlapping training windows re-run their shared cells; cell reuse across folds is a later optimisation, not part of this decision.
- The spec-path walk-forward (`app/research/walk_forward/`, split policies over a `StrategySpec`) is untouched and remains the canonical fixed-spec/train-selected evaluation over strategy specs; the study is the sweep-native counterpart.
