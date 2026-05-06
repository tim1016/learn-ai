---
id: F-0019
severity: P1
status: deferred
area: timestamp
canonical_file: PythonDataService/app/services/trade_comparison.py
reference: .claude/rules/numerical-rigor.md (Timestamp rigor → Ban list); docs/audits/computational-fidelity-2026-04-22.md (top-10 finding #2)
first_seen: 2026-05-05
last_seen: 2026-05-05
phase: 1
---

## What

`PythonDataService/app/services/trade_comparison.py:46-54` parses timestamp strings using:

```python
def _parse_ts(ts_str: str) -> float:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(ts_str.strip(), fmt).replace(tzinfo=UTC)
            return dt.timestamp()
        except ValueError:
            continue
```

Three of the four supported formats are **naive** (no timezone designator): `"%Y-%m-%d %H:%M"`, `"%Y-%m-%dT%H:%M:%S"`, `"%Y-%m-%d %H:%M:%S"`. The function then **silently coerces** all of them to UTC via `.replace(tzinfo=UTC)`.

This is the same anti-pattern flagged on the .NET side by `.claude/rules/numerical-rigor.md` ban list ("`DateTime.Parse(...)` ... causes the parser to silently coerce naive strings to UTC ... violates fail-fast ingestion") — applied here on the Python side.

The first format `"%Y-%m-%d %H:%M"` is identical to the format produced by `_format_timestamp` in `rule_based_backtest.py:252` per the prior audit's top-10 finding #2 (which observed that browsers parse it as local-time). So this `_parse_ts` is reading naive timestamps that the *prior audit already flagged as wrong on the producing side* and silently UTC-stamping them.

## Where

- `PythonDataService/app/services/trade_comparison.py:46-54` — `_parse_ts`
- Producers of naive timestamps that flow into this parser: per prior audit, `rule_based_backtest.py:252`, `strategies/common.py:115`

## Why this severity

P1 — Boundary timestamp violation in a comparison path. The math output (delta, match-rate, average-PnL-delta) all depend on these timestamps being correct. If the producer is in ET and the parser silently calls it UTC, every comparison result is shifted.

## Reproduction

```
grep -nA10 '_parse_ts' PythonDataService/app/services/trade_comparison.py
grep -n '_format_timestamp' PythonDataService/app/services/rule_based_backtest.py
grep -n 'sanitizer.py:216\|_format_timestamp' docs/audits/computational-fidelity-2026-04-22.md
```

## Suggested resolution (NOT auto-applied)

Per the timestamp policy:

1. **Fail-fast on naive timestamps.** Drop the three naive formats from `_parse_ts`. Accept only ISO-8601 with explicit offset, OR — preferred — accept `int` ms-epoch and skip string parsing entirely.
2. **Fix the producers** that emit naive strings (separate finding — falls under Phase 3 wider sweep). The producers should also stop emitting strings; the wire format is `int64 ms UTC`.

This finding is a symptom; the root cause is on the producer side. Both ends need the fix.

## Provenance of the finding itself

Phase 1 / cursor: `app/services/trade_comparison.py` head read. Cross-referenced with `.claude/rules/numerical-rigor.md` ban list and prior audit's top-10 finding #2.
