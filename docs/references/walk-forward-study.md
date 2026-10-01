# Walk-Forward Study

**Concept**: Answer the one question Grid Search cannot — *would the settings I would have picked have kept working?* — by repeating the selection step on the past. The range is cut into folds; in each fold the grid is swept over a training window, the fold winner is chosen there by the ranking contract, and the same grid is run over the following test window. Only the winner's test result is evidence; every other test cell is labelled exploratory. A frozen verdict is applied over the fold winners' retention of their training Sharpe. PRD: https://github.com/tim1016/learn-ai/issues/1925 (revision 7).

**Canonical implementation**: `PythonDataService/app/research/walk_forward_study/` (`folds.py` fold planning, `verdict.py` the frozen verdict, `service.py` the procedure over Grid Search's callable interface, `repository.py` + `models.py` the durable study record) with `app/research/walk_forward/metrics.py::sharpe_retention` / `median_fold_retention`; HTTP boundary `app/routers/walk_forward_study.py`; frontend `Frontend/src/app/components/walk-forward-study/`. Per-fold sweeps are ordinary Grid Search records (`owner_kind = 'walk_forward'`) and reuse everything in `docs/references/grid-search.md`. Storage decision: ADR 0055; procedure decision: ADR 0056.

## Decisions made while building (for review)

| Decision | Choice | Why |
|---|---|---|
| Study as a procedure over Grid Search | The study calls Grid Search's `prepare_launch` / `create` / `execute` per fold; fold sweeps are real `research_grid_searches` rows owned by the study. The attempt fence, the presented-status and Finish rules, and the router behaviour (refusals, liveness, cancel-and-await, presented-status filtering) are shared modules both features use; `StudySpec` composes a `GridSearchSpec`. | One sweep implementation, one receipt shape, one attempt fence, one Finish semantics; the study adds folds, selection and the verdict only. |
| Sweep ids persisted before a cell runs | The fold record stores the training (then test) sweep id before executing it. | A cancel inside a sweep otherwise leaves an orphan sweep and a Finish would launch a second one. |
| Exploratory marking | After the test sweep completes, `mark_exploratory` sets every test cell but the winner's. | The label is a fact about selection, known only once the training leader exists. |
| Fold failure is local | A fold's refusal is recorded on the fold with its code; the study continues; the verdict then reads `could not be judged`. | PRD: a failed fold breaks continuity but the record of the others is still worth keeping. |
| All folds failed | Study status `failed` with the reason; otherwise `completed` even with holes. | A study with nothing to show is a failure; a study with holes is a judged-as-unjudgeable result. |
| Month arithmetic | Start-anchored (`add_months` from the requested start), boundaries snapped to sessions; ends exclusive. | Deterministic folds for any start date; the calendar remains the session authority. |
| Estimate | Grid Search's estimate over the full range with `backtests_per_combination = 2 × folds`. | Errs long (each fold window is shorter than the range); labelled an estimate. |
| Winner drift | `winner_changes` counts how often the chosen settings moved between consecutive successful folds; shown, not judged. | The PRD asks that the drift be visible without folding it into the verdict. |
| Deleting a study | Deletes the study row and every sweep it owns in one transaction. | The sweeps have no meaning without the study, and never appear in Grid Search history. |

## Known limits

- The verdict reads Sharpe regardless of the ranking measure; a study ranked by net profit still judges retention of Sharpe (PRD).
- Fold windows share the study's frozen snapshot; a lake refresh between launch and a later fold fails that fold rather than re-freezing.
- The study does not reuse cells between overlapping training windows; each fold sweep runs its own cells.
