# C# scheduled jobs (retired, kept for reference)

The IV recorder cron and its helpers, lifted out of the C# backend when it was
removed (ADR 0081). They are **unused**: no project file, no build, no CI job
and no container includes them, and they will not compile on their own (they
reference `Backend.*` namespaces and Quartz). Last present in `Backend/` at
`origin/master` `2d932392`; `git log --follow` on a file here walks back into
its history.

They are kept because the scheduling idea is worth having when Python needs its
own clock, not because anything should run them. Nothing here is a source of
truth for math or schedules; `references/README.md` describes vendored upstream
code, this is first-party code kept for audit.

## What is here

| File | What it did |
|---|---|
| `IvRecorderRegistration.cs` | Registered a Quartz scheduler with one cron trigger per configured slot, in `America/New_York` (DST by Quartz), Monday to Friday. Opt-in: `IvRecorder:Enabled` was `false` in `appsettings.json` and compose never set it, so **it never ran in this stack**. `SlotToCronExpression` turned `"09:35"` into `0 35 9 ? * MON-FRI`. |
| `IvRecorderJob.cs` | The Quartz job each trigger ran. For each configured ticker it `POST`ed `{ticker, slot, target_calendar_days}` to Python `/api/iv-recorder/snapshot` and logged the outcome. One bad ticker did not stop the slot. `[DisallowConcurrentExecution]` stopped late slots piling up. No retries: the Python endpoint writes an error-tagged row itself when Polygon or the solver fails. |
| `IvRecorderOptions.cs` | Its config: `Enabled` (default `false`), `Tickers`, `Slots` (default 09:35 / 12:30 / 15:55 / 16:00 ET; 15:55 ran beside 16:00 for the trial month in ADR 0071 decision 7), `TargetCalendarDays` (30). The repo's `appsettings.json` set `Tickers` to SPY, QQQ, IWM, DIA, EFA. |
| `UnixMs.cs` | Not a job. The `DateTime` to `int64 ms UTC` helper the GraphQL layer used (`Backend/Temporal/`). Python's equivalent is `app/utils/timestamps.py` (`to_ms_utc`). Kept only because it sat in the folder the owner named. |
| `IvRecorderJobTests.cs` | The unit tests: one POST per ticker, errors tolerated, slot read from the job data, slot-to-cron mapping. Useful as a behaviour spec for a port. |

Wiring in the old `Program.cs` was one line: `builder.Services.AddIvRecorder(builder.Configuration);`.

## What stayed in Python

The cron only called Python; the work never left it. These are live:

- `PythonDataService/app/routers/iv_recorder.py`: `POST /api/iv-recorder/snapshot` (always 200; failures become an error-tagged row) and the series read route.
- `PythonDataService/app/services/iv_recorder.py`: `record_iv_snapshot`, `SLOT_CHOICES`, and the JSONL store (`get_iv_store`).

So the data path is intact; only the *clock* that called it is gone. A Python clock now records the closing slot (`PythonDataService/app/services/iv_recorder_schedule.py`, #3525); the 09:35, 12:30 and 15:55 slots stay unscheduled.

## Where a Python clock could plug in

Python has no general scheduler today. What it does have, in the order I would reach for it:

1. **A coroutine started from the app lifespan.** `PythonDataService/app/main.py` already starts long-lived background tasks at startup (`loop_lag_task`, the research schema ensure, the Alpaca reconnect task). A `slot clock` coroutine started in the same place, gated by `_ROLE_RUNS_DATA_PLANE_CORE` so only the coordinator or the combined process runs it (a clerk agent serves no data-plane routes), is the smallest port. It would call `record_iv_snapshot` directly through `asyncio.to_thread`, as the router does, instead of going through HTTP.
2. **The canonical calendar, not Monday to Friday.** The C# cron fired every weekday and let holidays write error rows. `PythonDataService/app/lean_sidecar/trading_calendar.py` (`is_trading_day`, `next_trading_day`, `session_open_ms_utc`, half-day aware session windows) is the single source of scheduled session structure (ADR 0022), and `app/services/session_authority.py` (`session_state_at_ms`) layers phases on it. A port should compute the next slot instant from those and skip closed days, with no hard-coded `time(9, 35)`. Slots are wall-clock ET offsets from the session open or close, converted through `ZoneInfo("America/New_York")`.
3. **One owner per fire.** Two data-plane processes must not both fire a slot. The research coordinator's singleton authority (`app/research/persistence/ownership.py`: `process_authority`) and the advisory-lock helper `app/utils/advisory_lock.py` are the existing ways to make one process the owner.
4. **As a visible job.** If a run should show in the Jobs panel, register a job type in the jobs facade (`app/jobs/facade.py`, `JOB_TYPE_ROUTES`) and run the work with `app/jobs/runner.run_in_thread` and `ProgressEmitter`, so the existing Redis state, SSE and cancel come for free. The options-archive nightly loop is the same shape of problem and would share the clock.
5. **Not worth it:** adding Quartz-like scheduling libraries (APScheduler, croniter). A slot is "an ET wall-clock offset on a trading day"; the calendar module answers that exactly and a cron expression cannot (half days).

When this is ported, update ADR 0071 decision 6 (it names the .NET cron as the owner) and delete this folder.
