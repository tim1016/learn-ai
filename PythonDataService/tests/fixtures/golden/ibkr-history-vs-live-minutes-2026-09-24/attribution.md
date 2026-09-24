# IBKR 1-minute history vs live-assembled minutes — SPY, 2026-09-24

Receipt for #2314: a resumed bot fills the hole after its retained bars from
IBKR 1-minute history. This fixture is the evidence that history reproduces
what the live feed would have delivered.

- **Reference (live):** `live_minutes.json` — the 457 `provenance=realtime`
  minutes the paper lane `paper-ema-spy-0924` (account PA3KWXU1C4C3) retained in
  `source_bars.sqlite3` on 2026-09-24, assembled by `MinuteAssembler` from IBKR
  `reqRealTimeBars` 5-second TRADES bars (09:32–17:08 ET, 388 of them RTH).
- **Candidate (history):** `history_minutes.json` — IBKR `reqHistoricalData`,
  SPY SMART/USD, `barSizeSetting="1 min"`, `whatToShow="TRADES"`,
  `useRTH=False`, `durationStr="6 D"`, `endDateTime=""`, fetched ~16:10 ET the
  same day through IB Gateway paper (client id 91, read-only), restricted to the
  live window.
- **Thin symbols:** `thin_symbol_completeness.json` — RTH minute counts and
  zero-volume counts from the same endpoint with `useRTH=True`, `1 D` ending
  2026-09-24 20:00 UTC.
- **Regenerate:** `python -m scripts.capture_ibkr_history_vs_live_fixture
  --ledger <clerk artifacts>/accounts/alpaca/paper:<sid>/source_bars.sqlite3
  --date 2026-09-24 --out tests/fixtures/golden/ibkr-history-vs-live-minutes-2026-09-24`
  inside a container that reaches IB Gateway. The ledger must hold that day's
  realtime minutes, and history for the day must still be served. The script
  asks for `1 D` ending 23:59 UTC of the date rather than the original `6 D`
  ending at fetch time; both cover the same minutes.
- **Tolerance:** bit-exact. Prices compare as `Decimal`, volume as integers.
- **Findings pinned by `tests/services/test_ibkr_history_equivalence_fixture.py`:**
  all 388 RTH minutes match exactly (OHLC and volume); 20 of the 69 extended-
  hours minutes differ by a cent in open or close (why an after-hours hole is
  refused); thin symbols get all 390 RTH minutes, zero-volume where nothing
  traded (why requiring every owed minute cannot lock one out).
- **Assumptions:** one day, one liquid symbol for the price comparison. Also
  measured, not pinned: 5-second history differs from realtime 5-second bars in
  210 of 4640, so the fill uses 1-minute bars; 5 days of history fetched at
  09:31 and re-fetched at 16:10 were identical (4171 bars).
