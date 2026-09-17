# SPY live EMA entry investigation — 2026-09-17

## Finding

`live-ema-spy-0917` started after the qualifying crossover. The retained IBKR
bars produce an ENTER on the 15-minute bar closing at **10:00 ET**. The run
launched at **10:01:28.134 ET**, 88.134 seconds later, and correctly consumed
that completed bar as warmup without placing an order. Subsequent bars stayed
above the slow EMA and did not produce another qualifying fresh crossover.

This explains the absence of a trade. The investigation found no indicator
readiness failure or post-launch signal discrepancy. This is not a claim of
bit-for-bit equality with TradingView: its underlying candle export and exact
indicator initialization were not supplied.

All human-readable times below are America/New_York (UTC−4 on this date),
confirmed by the user for the screenshot. The evidence retains integer UTC
milliseconds.

## Run and strategy

- Bot: `live-ema-spy-0917`, real-money Live account lane.
- Run: `126c2688f51348619ad0f86d011a64bc`.
- Launch: `1789653688134` ms UTC.
- Signal program: `ema_crossover_signal`, SPY, regular session, 15 minutes.
- Sealed parameters: EMA5/EMA10; Wilder RSI14, inclusive band 50–70;
  absolute EMA gap at least $0.20; normalized gap disabled; quantity 1.
- Data authority: IBKR; execution authority: Alpaca.

## Decisive bars

Numbers are rounded here for readability; replay compared full trace digests.

| Bar close ET | EMA5 | EMA10 | EMA5 − EMA10 | RSI14 | Decision and context |
|---|---:|---:|---:|---:|---|
| 09:45 | 756.081700 | 756.252632 | −0.170932 | 59.565869 | HOLD; fast EMA still below slow; pre-launch history |
| 10:00 | 757.804467 | 757.161244 | 0.643222 | 60.294152 | ENTER candidate; fresh crossover, gap and RSI pass; pre-launch history |
| 10:15 | 758.916311 | 757.884654 | 1.031657 | 59.894789 | HOLD; already above, no fresh crossover; first live decision |
| 10:30 | 759.837541 | 758.574717 | 1.262824 | 61.251640 | HOLD; already above, no fresh crossover |

The 10:00 close is `1789653600000` ms UTC. A chart candle labeled by its
opening time as 09:45 corresponds to this 10:00 decision. The 09:30–09:45
candle had not crossed on the bot's retained data. TradingView distinguishes
bar opening `time` and closing `time_close`; see its
[time documentation](https://www.tradingview.com/pine-script-docs/concepts/time/).
The screenshot alone cannot establish the exact intrabar crossing instant.

## Warmup and execution evidence

The retained ledger contains 4,593 one-minute source observations: 4,201
historical observations and 392 subsequent realtime observations. The history
includes extended hours for retention; the run's regular-session filter
admitted **1,591 historical minutes**, yielding **106 completed 15-minute
indicator updates** from September 11 through the September 17 10:00 close.

The sealed warmup request is five days. EMA5 needs five updates, EMA10 ten,
and RSI14 fifteen. All were ready before live decisions began. The warmup
also established that EMA5 was already above EMA10 at launch.

The live journal contains **24 no-action decisions**, from the 10:15 close
through the 16:00 close. It contains no ENTER, blocked entry, or quarantined
decision. No feed continuity event was recorded in this run's source ledger.
Replay through the production strategy seam, starting with the fresh run's
empty captured-decision history, reproduced **all 24 stored trace digests
exactly**. Thus these are matching decision contents, not just matching counts.

A counterfactual replay moved only the warmup/live cutoff to immediately
before today's regular-session open. It produced one ENTER candidate, at the
10:00 close. All candidates in this diagnostic were discarded in memory; no
broker API, order submission, or custody write was involved. This proves the
signal was available to an earlier-running strategy, not that a broker would
necessarily have accepted or filled an order.

## Why the bot did not enter late

The entry rule is **fresh crossover AND sufficient gap AND RSI in range**,
not merely **EMA5 above EMA10 AND RSI in range**. See
`PythonDataService/app/engine/strategy/algorithms/ema_crossover_signal.py:365`.
Each bar updates the previous-above state, including warmup bars.

Warmup deliberately discards historical, unowned entry candidates and starts
the strategy flat. See
`PythonDataService/app/services/bot_trade_strategy_warmup.py:154` and `:167`.
The $0.20 gap did not block the 10:00 signal: the gap was approximately $0.6432.

For this existing strategy, the operational remedy is to have the bot running
and warmed before the desired trading window. Entering an already-existing
bullish condition at startup would change strategy semantics and requires a
separately specified and validated rule.

## Verification and scope

Read-only diagnostic invocation during this investigation:

```sh
podman exec -i alpaca-live-clerk python < /tmp/spy_ema_diagnosis_20260917.py > /tmp/spy_ema_diagnosis_20260917.json
```

These temporary local artifacts are investigation aids, not committed golden
fixtures. The script reads the instance's immutable launch/seal records, the
source SQLite ledger in `mode=ro`, and the account's decision receipts in
`mode=ro`. It uses the registered program through `strategy_evaluations` and
the production runtime/consolidation path for warmup inspection.

Result: `PASS: 24/24 exact trace digests; zero live entry candidates;
earlier-launch entry at 10:00 ET`.

Focused existing tests, run from `PythonDataService/`:

```sh
.venv/bin/python -m pytest \
  tests/fixtures/test_indicator_fixtures.py::TestIND001EMA \
  tests/fixtures/test_indicator_fixtures.py::TestIND003RSI \
  tests/engine/strategy/algorithms/test_signal_only_ema_crossover.py \
  tests/services/test_bot_trade_strategy_warmup.py -q
```

Result: **26 passed in 0.53 seconds**. These cover canonical EMA/RSI fixture
values and readiness, signal gates, and warmup policy; they are not a direct
comparison against the user's TradingView candle series.

No production code, bot state, configuration, orders, or live data-provider
settings were changed. The bot was already stopped when inspected.

## Separate replay-report observation

The saved end-of-run replay receipt classified the pre-launch 10:00 candidate
as `MISSING_LIVE_RECORD`. Passing the later 24 captured decisions into a
retrospective replay reproduces a false `crash_recovered` candidate for that
pre-launch bar; passing the empty captured history actually present at fresh
launch produces exactly the 24 recorded live decisions. The operational
missed-entry finding above therefore does not rely on the saved replay
receipt's overall `drift` verdict. The open reporting defect is tracked in
[Known Gaps](../known-gaps.md#live-ema-replay-evidence--verified-2026-09-17).

The saved receipt also has a separate engine-parity sequence-exhaustion
failure. Its cause was not established here; this investigation instead
verified the live decision contents against the same production strategy
seam and ran the focused canonical indicator tests.
