# IBKR 1-minute history settle time after a minute closes

- **Reference:** IB Gateway (paper, port 4002) `reqHistoricalData`, 1 min TRADES,
  `useRTH=False`, `durationStr="180 S"`, SMART routing.
- **Captured:** 2026-09-24, 19:33–19:41 ET (extended hours, post-market), SPY and TSLA,
  8 consecutive minutes (16 symbol-minutes).
- **Command:** `PythonDataService/scripts/probe_ibkr_history_settle.py --symbols SPY,TSLA --minutes 8`
  run inside `polygon-data-service` (see the script's docstring).
- **Fields:** `first_row_s` / `first_final_s` are seconds after the minute's close at
  which its row first appeared / first held the value it still held 50 s later;
  `max_req_s` is the slowest request; `changed_after_close` says the row was revised.
- **Assumptions:** polls about once a second, so a settle time is known only to about
  1 s. Post-market only: regular-hours minutes are busier and were not measured.
- **Use:** justifies `STARTUP_JOIN_SETTLE_MS` (5 s) in `app/config.py` (#2410).
