# Direct One SPY EMA crossover native-feed receipt

## Classification

**Kind:** vendor-observed native-feed execution receipt. It is an operational
validation of the default SPY EMA crossover's filled-order and duration
profile, not a strict same-input Python ↔ LEAN parity oracle. The Direct One
export does not contain the minute bars, fifteen-minute indicator states, or
signal sequence needed to distinguish a data-feed difference from an engine
difference.

## Source and configuration

- **Execution source:** the user-supplied `SpyEmaCrossoverAlgorithm` observer
  copy, SHA-256
  `c305d20b9ec3e994ae3ba5a21d9b8226dd3f3e65ebbd13ad6355c9695fe72a3a`.
  It is semantically identical to the checked-in observer algorithm for the
  EMA, RSI, crossover, all-in sizing, and five-bar exit rules. Its only source
  difference is its date floor: 2025-01-01 → 2026-01-01.
- **Run configuration:** SPY at minute resolution, $100,000 initial cash,
  2025-01-01 → 2026-01-01; empty parameter map means the attached algorithm's
  defaults were used.
- **Upstream result export:** `Emotional Violet Owlet.json`, SHA-256
  `faa6eea7c312fef1c6fca4a537bd3c34b0b5517d7f0a34fbc16dfe197155a391`.
- **Raw order export:** `orders.csv`, SHA-256
  `7761f506d0de3d56c39005ba2eb13c54e47d700ed109de8df7f420f0c8a3b479`.
- **Direct One / QuantConnect run id:** not present in the supplied exports.

## Normalization

`orders.csv` is the retained vendor export and intentionally keeps its ISO
timestamps. `closed_trades.csv` is deterministically normalized from
`totalPerformance.closedTrades` in the JSON result: it uses canonical `int64`
milliseconds UTC for entry and exit time, whole-share quantities, and
two-decimal fees. Regenerate it only from a new, identified Direct One export:

```sh
jq -r '.totalPerformance.closedTrades[] | [(.entryTime | fromdateiso8601 * 1000), (.exitTime | fromdateiso8601 * 1000), .entryPrice, .exitPrice, .quantity, .profitLoss, .totalFees, .duration, (.orderIds | join(";"))] | @tsv' \
  'Emotional Violet Owlet.json' | awk -F '\t' 'BEGIN { print "entry_time_ms,exit_time_ms,entry_price,exit_price,quantity,profit_loss,total_fees,reported_duration,order_ids" } { printf "%s,%s,%s,%s,%d,%s,%.2f,%s,%s\\n", $1, $2, $3, $4, $5, $6, $7, $8, $9 }' > closed_trades.csv
```

Do not alter individual rows by hand.

## Observed duration profile

- 36 closed long SPY trades / 72 filled orders.
- 29 trades have a 75-minute fill-to-fill duration.
- Three market-on-open entries yield a 74-minute fill-to-fill duration.
- Three positions cross the close with an 18:45 elapsed duration.
- One market-on-open exit after the close yields an 18:46 elapsed duration.

The last three categories are fill-clock artifacts at the regular-session
boundary. They do not change the five consolidated-bar strategy clock. The
regression test is
`PythonDataService/tests/integration/reconciliation/test_spy_ema_crossover_direct_one.py`.
