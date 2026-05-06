---
id: F-0033
severity: P1
status: deferred
area: timestamp
canonical_file: PythonDataService (cross-cutting, non-ingestion)
reference: .claude/rules/numerical-rigor.md (Timestamp rigor → Ban list)
first_seen: 2026-05-06
last_seen: 2026-05-06
phase: 3
---

## What

Phase 3 per-file triage of the Python non-ingestion ban-list candidates from F-0020. **10 files confirmed in violation** across services / research / engine / routers. Most are isolated single-line issues; aggregated here to avoid finding-doc proliferation. The pattern is consistent: ISO-Z string emission for wire/persistence, plus naive `datetime.now()` / `datetime.utcnow()`.

## Where

### Confirmed violations

| File | Line | Pattern | Note |
|---|---|---|---|
| `app/services/options_companion_service.py` | 418 | `datetime.fromtimestamp(ts/1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")` | tz-aware datetime → ISO-Z. Same anti-pattern as F-0009. |
| `app/research/options/iv_builder.py` | 82 | `datetime.utcfromtimestamp(ts/1000)` | Banned API. |
| `app/research/options/iv_builder.py` | 413 | `pd.to_datetime(df["date"])` | **No `utc=True`** — produces naive. |
| `app/research/options/iv_builder.py` | 417 | `df[col].ffill(limit=MAX_FFILL_DAYS)` | Forward-fill in research path; bounded but silent. |
| `app/research/options/contract_finder.py` | 128 | `pd.to_datetime(stock_bars["date"]).sort_values().unique()` | **No `utc=True`** — produces naive. |
| `app/research/options/contract_finder.py` | 263 | `datetime.utcfromtimestamp(ts/1000)` | Banned API. |
| `app/engine/framework/insight.py` | 86 | `self.updated_time_utc = datetime.utcnow()` | Banned API. |
| `app/engine/strategy/algorithms/spy_ema_crossover_options.py` | 615 | `time=self.ctx.current_time or datetime.now()` | Naive `datetime.now()`. |
| `app/routers/validation_study.py` | 609 | `datetime.fromtimestamp(ts/1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")` | Same as options_companion. |
| `app/routers/volatility.py` | 538, 554, 690, 740 | `datetime.now().strftime("%Y-%m-%d")` | 4 occurrences of naive `datetime.now()`. |
| `app/research/divergence/dashboard/build_dashboard.py` | 309 | `datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")` | Banned API; metadata only — P2. |
| `app/research/divergence/strategies/engine_runner.py` | 68 | `df["iso_time"] = df["time_utc"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")` | Same Z-emission as F-0009. |
| `app/volatility/cache.py` | 173 | `"built_at": datetime.utcnow().isoformat() + "Z"` | Banned API + naive Z-lie pattern. |

### Verified clean

- `app/services/market_monitor.py:209` — `dt.strftime("%a %b %d %Y, %I:%M %p %Z")` — display formatting only, not on ban list.

## Why this severity

P1 — Same anti-pattern as F-0009, F-0019, F-0024, just in additional locations. Mostly the same fix (move to `int64 ms UTC` wire format; replace `datetime.utcnow` with `datetime.now(UTC)`; use `pd.to_datetime(..., utc=True)` for parse).

The `iv_builder.py` `pd.to_datetime` calls are particularly relevant since they're in the IV-construction path (registry calls IV term-structure interpolation `pending-fixture` already; producing naive-tz dates contributes to that fragility).

## Reproduction

```
grep -rnE 'datetime\.utcnow|datetime\.utcfromtimestamp|\.strftime\(["'"'"'].*Z["'"'"']|datetime\.now\(\)' PythonDataService/app/services/ PythonDataService/app/research/ PythonDataService/app/engine/framework/ PythonDataService/app/engine/strategy/algorithms/ PythonDataService/app/routers/ PythonDataService/app/volatility/
```

## Suggested resolution (NOT auto-applied)

Group the fixes by consumer:

1. **ISO-Z emissions for wire / response-envelope** (5 occurrences across `options_companion_service.py`, `validation_study.py`, `engine_runner.py`, `cache.py`, `build_dashboard.py`) — convert to `int64 ms UTC` wire format or drop the string entirely.

2. **`datetime.utcnow()` / `datetime.utcfromtimestamp()` calls** (5 occurrences) — replace with `datetime.now(UTC)` and `datetime.fromtimestamp(ts, tz=UTC)`.

3. **`pd.to_datetime(...)` without `utc=True`** (`iv_builder.py:413`, `contract_finder.py:128`) — add `utc=True`. **These are in IV-research paths**; verify whether the dates are already UTC midnight or genuinely date-only — both interpretations work but consumers downstream may behave differently if naive.

4. **Bounded forward-fill** (`iv_builder.py:417`) — distinct from F-0023 (unbounded forward-fill in ingestion). Bounded fill in research is more defensible; document the choice (`MAX_FFILL_DAYS = ?`) in the registry's IV-builder row.

5. **Naive `datetime.now()` for default expiry/eval dates** (`volatility.py` x4, `spy_ema_crossover_options.py`) — accept the date as a parameter or use `date.today()` if a UTC-equivalent date is intended; document timezone assumption.

## Provenance of the finding itself

Phase 3 / cursor: per-file batched grep of the 14 candidate Python files from F-0020. Found 10 with confirmed ban-list pattern violations.
