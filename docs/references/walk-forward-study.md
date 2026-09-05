# Walk-Forward Study

**Concept**: Answer the one question Grid Search cannot — *would the settings I would have picked have kept working?* — by repeating the selection step on the past. The range is cut into folds; in each fold the grid is swept over a training window, the fold winner is chosen there by the ranking contract, and the same grid is run over the following test window. Only the winner's test result is evidence; every other test cell is labelled exploratory. A frozen verdict is applied over the fold winners' retention of their training Sharpe. PRD: https://github.com/tim1016/learn-ai/issues/1925 (revision 7).

**Canonical implementation**: `PythonDataService/app/research/walk_forward_study/` (`folds.py` fold planning, `verdict.py` the frozen verdict, `service.py` the procedure over Grid Search's callable interface, `repository.py` + `models.py` the durable study record) with `app/research/walk_forward/metrics.py::sharpe_retention` / `median_fold_retention`; HTTP boundary `app/routers/walk_forward_study.py`; frontend `Frontend/src/app/components/walk-forward-study/`. Per-fold sweeps are ordinary Grid Search records (`owner_kind = 'walk_forward'`) and reuse everything in `docs/references/grid-search.md`. Storage decision: ADR 0055; procedure decision: ADR 0056.

**Validated against**: `tests/research/walk_forward_study/test_{folds,verdict,service}.py` (service against an ephemeral Postgres with a fake engine keyed by phase, fold and setting); `tests/routers/test_walk_forward_study_endpoints.py`; `tests/research/walk_forward/test_metrics.py`; Angular specs beside each component.

## The procedure

1. **Plan the folds.** Month arithmetic is anchored on the requested start date; each boundary snaps forward to the next trading session. Folds step by the test length, so every month after the first training window is scored exactly once. A range that does not divide into whole folds is refused with the nearest valid end dates (`FOLDS_INVALID`).
2. **Admit on the whole workload.** `combinations × folds × 2 ≤ 5,000` backtests, the same limit Grid Search applies to one sweep.
3. **Freeze once.** The study captures one data snapshot over the whole range and one code identity at launch. Every fold sweep captures its own window's snapshot and it must agree byte-for-byte with the study's, or that fold fails (`DATA_SNAPSHOT_MISMATCH`) rather than running on other bytes.
4. **Per fold:** launch the training sweep (owned, `phase = 'train'`), persist its id, run it; take the leader from the ranking contract (no leader → `NO_ELIGIBLE_CANDIDATE`); launch and run the test sweep (`phase = 'test'`), mark every cell but the winner's exploratory; a failed winner test cell fails the fold (`WINNER_TEST_FAILED`). Every window — training and test, winner and exploratory — gets the same uniform run-up because Grid Search sizes one for every sweep.
5. **Verdict** over the fold winners, then `completed`; only a study whose every fold failed is `failed`.

## The frozen verdict (`verdict.py`)

| Rule | Outcome |
|---|---|
| Any fold failed, or no fold succeeded | `could not be judged` — the out-of-sample record has holes |
| No fold has a defined retention (every training Sharpe non-positive or null) | `could not be judged` |
| Defined retentions `D < ceil(S / 2)` of `S` successful folds | `could not be judged` — fewer than half can be judged |
| Out-of-sample trades across fold winners `< min_trades` | `too few trades` |
| Median test Sharpe `≤ 0` | `stopped working` |
| Median fold retention `≥ 0.5` | `still worked` |
| otherwise | `got worse` |

Retention per fold is `test_sharpe / train_sharpe`, defined only when both are finite and the training Sharpe is positive; the study figure is the **median** over defined retentions. Coverage is always disclosed as "based on D of S folds". The threshold, the coverage floor and the median are judgment calls recorded in the PRD; the counterexamples that forced the median and the coverage rule are reproduced in `test_verdict.py`.

## Decisions made while building (for review)

| Decision | Choice | Why |
|---|---|---|
| Study as a procedure over Grid Search | The study calls Grid Search's `prepare_launch` / `create` / `execute` per fold; fold sweeps are real `research_grid_searches` rows owned by the study. | One sweep implementation, one receipt shape, one attempt fence, one Finish semantics; the study adds folds, selection and the verdict only. |
| Sweep ids persisted before a cell runs | The fold record stores the training (then test) sweep id before executing it. | A cancel inside a sweep otherwise leaves an orphan sweep and a Finish would launch a second one. |
| Exploratory marking | After the test sweep completes, `mark_exploratory` sets every test cell but the winner's. | The label is a fact about selection, known only once the training leader exists. |
| Fold failure is local | A fold's refusal is recorded on the fold with its code; the study continues; the verdict then reads `could not be judged`. | PRD: a failed fold breaks continuity but the record of the others is still worth keeping. |
| All folds failed | Study status `failed` with the reason; otherwise `completed` even with holes. | A study with nothing to show is a failure; a study with holes is a judged-as-unjudgeable result. |
| Month arithmetic | Start-anchored (`add_months` from the requested start), boundaries snapped to sessions; ends exclusive. | Deterministic folds for any start date; the calendar remains the session authority. |
| Estimate | Grid Search's estimate over the full range with `backtests_per_combination = 2 × folds`. | Errs long (each fold window is shorter than the range); labelled an estimate. |
| Winner drift | `winner_changes` counts how often the chosen settings moved between consecutive successful folds; shown, not judged. | The PRD asks that the drift be visible without folding it into the verdict. |
| Deleting a study | Deletes the study row and every sweep it owns in one transaction. | The sweeps have no meaning without the study, and never appear in Grid Search history. |
| Retirement | The SPY EMA normalized-gap protocol page, its job types (`spy_ema_walk_forward`, `spy_ema_exhaustive`), the Exhaustive Run package, routers, schemas, tests, frontend pages and user manual are removed. On-disk artifacts under `artifacts/walk-forward/` and `artifacts/exhaustive-runs/` were **not** deleted. | PRD #1925 retires them; deleting stored research evidence is the operator's call. |

## Known limits

- The verdict reads Sharpe regardless of the ranking measure; a study ranked by net profit still judges retention of Sharpe (PRD).
- Fold windows share the study's frozen snapshot; a lake refresh between launch and a later fold fails that fold rather than re-freezing.
- The study does not reuse cells between overlapping training windows; each fold sweep runs its own cells.
