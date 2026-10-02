# EXP-001 — EMA decision explanations, SPY, 2026-09-29

**What it pins.** The EMA Crossover Signal program's decision explanation
(#2639) on the two decision bars the 2026-09-29 missed-trade investigation
turned on: the 15-minute bars that closed at 14:30 and 14:45 ET.

**Input (`input.json`).** The live bot `spy-ema-20260929-1432`'s own retained
IBKR one-minute SPY bars, regular session only (`session_phase = 'RTH'`), from
2026-09-23 through 2026-09-29: 1,950 bars, prices as the ledger stored them
(decimal strings). Read from that bot's ledger,
`accounts/alpaca/live-evidence:spy-ema-20260929-1432/source_bars.sqlite3` in
the live Clerk's artifacts volume, copied out read-only on 2026-10-01. This
is the bar stream the live runner consolidated: it decides on regular-session
bars only (`use_rth=true`).

**Output (`output.json`).** The canonical program's explanation on each of the
two bars, replayed with the bot's deployed settings (the registered defaults:
gap 0.20, gap_bps 0, RSI 50–70, EMA 5/10, hold 5) from the first retained
session.

| Bar close (ET) | Fresh cross | Gap (needs ≥ 0.20) | RSI (needs 50–70) |
|---|---|---|---|
| 14:30 | yes | 0.0262 ✗ | 47.26 ✗ |
| 14:45 | no, already above | 0.2313 ✓ | 53.29 ✓ |

**Reference.** Internal regression: the oracle is the canonical program
itself (`app/engine/strategy/algorithms/ema_crossover_signal.py`). The values
agree with the independent terminal replay done during the 2026-09-29
investigation (recorded on issue #2639): a 14:30 gap of 0.0262–0.0263 and RSI
47.19–47.31, stable whether warmup started on Sep 23, 25 or 28.

**Tolerance.** Values `atol=1e-9, rtol=0` (float64 from exact `Decimal`
indicator math); pass/fail and state tokens exact.

**Regenerate.** From `PythonDataService/`:

```
.venv/bin/python -m scripts.fixture_generators.ema_decision_explanation_2026_09_29 [--ledger <source_bars.sqlite3>]
```

Without `--ledger` it replays the committed `input.json` and rewrites only
`output.json`.
