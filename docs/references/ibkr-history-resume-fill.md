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

**Limits.** One day, one liquid symbol for the price comparison. The fetch is
clamped to the sealed warmup lookback (10 days of 1-minute history returned in
0.6 s; 20 days took 48 s, beyond `_HISTORICAL_BARS_TIMEOUT_S`). Re-capture on a
second symbol and day before widening the claim.

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
