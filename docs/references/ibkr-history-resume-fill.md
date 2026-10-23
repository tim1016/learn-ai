# IBKR 1-minute history as the fill for a resumed run's hole (#2314)

**What it decides.** A resumed bot warms on the bars its earlier runs retained,
then fills the minutes that passed while it was stopped from IBKR
`reqHistoricalData` 1-minute TRADES bars before it decides anything
(`app/services/retained_tail_join.py`). A hole it cannot fill faithfully refuses
the run: `RESUME_HOLE_AFTER_HOURS` when the run decides on an extended-hours
minute anywhere in the hole, `RESUME_HOLE_UNFILLED` when history lacks an owed
regular-hours minute.

**Why history is admissible.** It is not a port of external math; it is a claim
that a second IBKR endpoint reproduces the first. The receipt is a captured
fixture and a verifier:

- Fixture: `PythonDataService/tests/fixtures/golden/ibkr-history-vs-live-minutes-2026-09-24/`
  (attribution, capture command, and the three data files).
- Verifier: `PythonDataService/tests/services/test_ibkr_history_equivalence_fixture.py`.
- Capture script: `PythonDataService/scripts/capture_ibkr_history_vs_live_fixture.py`.

| Claim | Measured (SPY, 2026-09-24) | Tolerance |
|---|---|---|
| Regular-hours minutes are reproduced | 388 of 388 identical, OHLC and volume | bit-exact |
| Extended-hours minutes are not | 20 of 69 differ by $0.01 in open or close | — (refused) |
| Thin symbols return every regular minute | EWN, FLCH, KBWP: 390 of 390, 314–371 zero-volume | exact count |
| History is stable after the fact | 4171 bars fetched 09:31 and 16:10 identical | bit-exact (not pinned) |
| 5-second history is not a substitute | 210 of 4640 differ | — (not used) |

**Limits.** One day, one liquid symbol for the price comparison.

**`N D` counts trading sessions (#2849).** Read-only probe, IB Gateway Paper,
SPY 1-minute `TRADES`, explicit end times, 2026-10-03:

| Request (end time, duration) | Bars | Sessions returned | Took |
|---|---|---|---|
| Mon 2026-09-28 09:31 ET, `5 D`, extended hours | 4171 | 5 (Tue 22 to Fri 25 in full, Mon 28 to 09:30) | 1.4 s |
| Thu 2026-10-01 09:31 ET, `5 D`, extended hours | 4171 | 5 (Fri 25 to Wed 30 in full, Thu 1 to 09:30) | 1.6 s |
| Mon 2026-09-28 09:31 ET, `5 D`, regular hours | 1561 | 5 (4 × 390 + 1) | 0.2 s |
| Mon 2026-09-28 09:31 ET, `10 D`, extended hours | 8971 | 10 | 33.9 s |
| Mon 2026-09-28 09:31 ET, `15 D`, extended hours | 13771 | 15 | 32.0 s |
| Mon 2026-09-28 09:31 ET, `20 D`, extended hours | 18571 | 20 | 56.2 s |

So the warmup lookback is a count of completed sessions, and it asks for one
more (`N+1 D`) for the session in progress. A request of more than five
sessions is given 75 s instead of 15 s; nothing between 5 and 10 is measured.
The ceiling is 19 completed sessions, which is the `20 D` that was measured to
return. One symbol; re-probe a second before widening the claim.

## The startup join (#2410)

The same endpoint fills the minute a run's live stream joined partway through,
so warmup and live meet without a hole (`app/services/startup_join.py`). The
extra claim is about *when* a just-closed minute can be asked for:

- Fixture: `PythonDataService/tests/fixtures/golden/ibkr-history-settle-2026-09-24/`.
- Verifier: `PythonDataService/tests/services/test_ibkr_history_settle_fixture.py`.
- Probe: `PythonDataService/scripts/probe_ibkr_history_settle.py`.

| Claim | Measured (SPY, TSLA, 2026-09-24 19:33–19:41 ET) | Consequence |
|---|---|---|
| The closed minute's row is served at once | 16 of 16 within 0.12 s of the close | presence proves nothing |
| The row can still change after the close | 3 of 16 revised, final by 1.32 s | ask after `STARTUP_JOIN_SETTLE_MS` = 5 s |
| A request is fast | slowest 1.06 s | well inside the 180 s budget |

**Limits.** Post-market only, 8 minutes, polled about once a second. Re-run the
probe at a regular-hours open before relying on the settle time there; it is a
setting, not a constant.

**Standing decisions:** ADR 0053 §10 amendment (2026-09-24, #2314) and §13
amendment (2026-09-24, #2410).
