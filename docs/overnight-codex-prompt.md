# Overnight Execution Brief — IBKR Paper-Trading Live Runtime

You are running unsupervised overnight. The user (Inkant) is asleep
and will not respond. Your job is to make as much verified progress
as possible on the plan at `docs/ibkr-paper-deployment-plan.md` and
leave a clean, reviewable state in the morning.

This is a different operating mode from the standard execution brief:
**continue through phases without waiting for authorization**, but
respect the hard gates and stop conditions below.

---

## Required reading (do this first; do not skip)

1. `docs/ibkr-paper-deployment-plan.md` — full plan, cover to cover
2. `docs/architecture/ibkr-integration-tdd.md` — the existing IBKR module you depend on
3. `CLAUDE.md`
4. `.claude/rules/python.md`
5. `.claude/rules/numerical-rigor.md`
6. `.claude/rules/testing.md`

Then read the files whose patterns you must follow:
`app/broker/ibkr/{client,config,orders,market_data,account,models,contracts}.py`,
`app/engine/{engine.py,strategy/base.py,strategy/algorithms/spy_ema_crossover.py,
execution/portfolio.py,execution/order.py,execution/fill_model.py,
consolidators/trade_bar_consolidator.py,framework/insight_manager.py,
tests/test_spy_next_bar_open_validation.py}`,
`requirements-light.txt` (confirm `ib_async>=2.0,<3.0` is already there — do NOT add deps).

---

## Scope: tonight ends at Phase 7

Run Phases 1 through 7 sequentially. **Do not start Phase 8 or Phase 9.**
Those require human-side decisions (config defaults, paper account
specifics) and will be done tomorrow morning with Inkant present.

Phase 6 (replay parity) is the priority. If you have to choose between
Phase 6 polish and Phase 7 progress, choose Phase 6. Phase 6 passing
is the demo headline; Phase 7 is hardening.

---

## Environment

Run all `ruff` and `pytest` commands from `PythonDataService/` with the
local `.venv` active — **not** inside `podman exec`. The user's venv
is at `PythonDataService/.venv/`. If `ruff` / `pytest` aren't installed
there: `pip install -r requirements-dev.txt`.

Standard commands you will use repeatedly:

```bash
cd PythonDataService
source .venv/bin/activate     # or .venv\Scripts\activate on Windows

# Project-scope lint (must be clean after every phase):
ruff check app/ tests/

# Targeted phase tests (example — substitute the phase's tests):
pytest tests/engine/live/ -x

# Pre-existing tests that must keep passing:
pytest tests/broker/ibkr/ \
       app/engine/tests/test_spy_validation.py \
       app/engine/tests/test_spy_next_bar_open_validation.py -x
```

## Continuous-execution rules

After completing each phase:

1. Run the phase's tests with `pytest <phase-test-path> -x`. They must pass.
2. Run `ruff check app/ tests/` from `PythonDataService/`. Must be clean.
3. Run the pre-existing-tests command above to confirm you didn't
   regress anything outside the live runtime.
4. `git add` the phase's files and commit with message
   `phase N: <short summary>`.
5. Push to `overnight/ibkr-paper-runtime-<date>` branch.
6. Append a phase summary to `docs/overnight-progress.md`
   (create on first write):
   - Files created/modified (with line counts)
   - Test count and pass/fail
   - Wall-clock time
   - Any deviations from the plan with justification
   - Any flags
7. **Continue to the next phase immediately** — do NOT wait for
   authorization. The user is asleep.

---

## Hard stop conditions — write the diagnostic and exit

You must stop and exit the run (cleanly) if any of these occur:

1. **Hard gate failure.** Phase 6 (replay parity) or Phase 7
   (collapse test) does not pass exactly. The replay test demands
   `atol=Decimal("0")` on fill price, fees, and trade-log entries.
   Do **not** loosen tolerances to make a test pass. Commit what
   passed cleanly, write a diagnostic at `docs/overnight-diagnostic.md`
   (root cause hypothesis + reproduction + 2-3 candidate fixes), exit.

2. **Pre-existing failures on the baseline.** Before you start Phase 1,
   confirm baseline is clean from `PythonDataService/` with `.venv`
   active:
   ```
   ruff check app/ tests/
   pytest tests/broker/ibkr/ \
          app/engine/tests/test_spy_validation.py \
          app/engine/tests/test_spy_next_bar_open_validation.py -x
   ```
   If `ruff` is dirty on `main`, or if any of those tests fail on
   `main`, **do not start the implementation**. Write a one-paragraph
   note to `docs/overnight-diagnostic.md` and exit. The user will fix
   baseline and restart you.

3. **Plan-vs-reality mismatch.** If a file the plan expects doesn't
   exist, or its actual surface differs materially from what the
   plan describes, stop. Document at `docs/overnight-diagnostic.md`,
   exit. Do not improvise.

4. **An assertion in the plan can't be implemented as written.**
   Stop. Document. Exit. Do not silently weaken assertions.

5. **The forbidden-files list is challenged.** If you genuinely
   believe you need to modify
   `app/engine/strategy/algorithms/spy_ema_crossover.py`,
   `app/engine/engine.py`,
   `app/engine/strategy/base.py`,
   or any existing file under `app/broker/ibkr/` other than
   `models.py` (one new model only), stop. The plan is wrong if
   this is true and only a human can decide.

6. **Time budget exceeded for a single phase.** If a single phase
   takes more than 2.5 hours of wall-clock without converging,
   commit what's progressed, document at the diagnostic file
   ("Phase N stalled at <step>; <hypothesis>"), exit.

When you exit cleanly under any of these conditions, **always**:
- Make sure the working tree is clean (commit or stash WIP).
- Push the branch.
- Append a final entry to `docs/overnight-progress.md` summarizing
  what was completed and what wasn't.

---

## Forbidden actions

- **Do not modify** `app/engine/strategy/algorithms/spy_ema_crossover.py`,
  `app/engine/engine.py`, `app/engine/strategy/base.py`. The strategy
  class and `BacktestEngine` are unchanged by design;
  `StrategyContext` is the contract.
- **Do not modify** any file under `app/broker/ibkr/` except adding
  `bars.py` and one new model (`IbkrMinuteBar`) in `models.py`.
- **Do not add** to `requirements-light.txt` or `requirements-heavy.txt`.
- **Do not use** `# type: ignore` to silence type errors.
- **Do not use** bare `except:` or `except Exception: pass`.
- **Do not use** ISO strings, naive `datetime`, or `pd.Timestamp` as
  wire/storage timestamp formats. `int64 ms UTC` at all boundaries.
- **Do not use** `np.allclose` / `np.isclose` with default tolerances.
- **Do not substitute** `MagicMock` where the plan specifies a
  deterministic `FakeBroker`.
- **Do not skip** ahead to Phase 8 or 9.
- **Do not loosen test assertions** to make tests pass. Loosened
  tolerances on the replay parity test invalidate the entire receipt.
- **Do not commit secrets, API keys, or `.env` contents.**

---

## Status logging

Every 30 minutes during execution, append a one-line heartbeat to
`docs/overnight-progress.md`:

```
[HH:MM] Phase N step <step>: <one-line status>
```

This lets the user see at a glance where the night went when they
wake up. If you're stalled, the heartbeats will show it.

---

## First move

Before reading anything else, post in the terminal a one-page
kickoff that proves you read this brief:

1. Confirm you understand: continuous execution through Phase 7,
   hard gates respected, no Phase 8/9.
2. List the first 3 files you'll create in Phase 1.
3. State the time you started.
4. Confirm the baseline (main branch ruff + tests) is clean. If
   not, exit per stop condition 2.

Then begin Phase 1.

---

## Morning deliverable

When the user wakes up, the state of the repo should be:

- Branch `overnight/ibkr-paper-runtime-<date>` pushed to origin.
- One commit per completed phase.
- `docs/overnight-progress.md` with phase summaries + heartbeats.
- `docs/overnight-diagnostic.md` IF anything stopped the run early.
- All tests in scope passing (run from `PythonDataService/` with
  `.venv` active).
- `ruff check app/ tests/` clean (run from `PythonDataService/`).
- No uncommitted changes in the working tree.

That is the demo. The user will:
1. Read `overnight-progress.md`.
2. Run the replay parity test (Phase 6) themselves.
3. Show their boss the test output.

If you achieve Phases 1-6 with the replay test passing exactly,
the demo is solid. Phase 7 is bonus.
