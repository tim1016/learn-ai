---
id: F-0024
severity: P1
status: fixed-verified
area: timestamp
canonical_file: PythonDataService/app/research/divergence/ingest/polygon_ingest.py; PythonDataService/app/services/dataset_service.py; PythonDataService/app/services/polygon_client.py
reference: .claude/rules/numerical-rigor.md (Timestamp rigor → Ban list)
first_seen: 2026-05-06
last_seen: 2026-05-06
phase: 3
---

## What

Three more ban-list violations confirmed in the Python ingestion-path triage of F-0020's candidates:

### `polygon_ingest.py:226` — ISO-Z emission

```python
df["iso_time"] = pd.to_datetime(df["unix_ts"], unit="ms", utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
```

Same pattern as F-0009 (sanitizer.py:79). An ISO-Z string column is built and may be persisted / consumed downstream — but the wire format should be `int64 ms UTC`, not a string. Same fix.

### `dataset_service.py:851` — banned `datetime.utcfromtimestamp`

```python
iso = datetime.utcfromtimestamp(ts / 1000).strftime("%Y-%m-%dT%H:%M:%SZ")
```

`datetime.utcfromtimestamp` is on the explicit ban list per `.claude/rules/numerical-rigor.md` → Timestamp rigor → Ban list ("`datetime.utcfromtimestamp` — same" as `datetime.utcnow`). Returns naive UTC; emits naive Z-suffixed ISO string.

### `dataset_service.py:939, :1139` — banned `datetime.utcnow`

```python
"generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
```

`datetime.utcnow` is on the explicit ban list. These are used for response-envelope `generated_at` metadata, **not** for data itself, so impact is lower — but still banned.

### `polygon_client.py:625, :628, :676` — naive `datetime.now()` for date filters

```python
today = datetime.now().strftime("%Y-%m-%d")
expiration_date_lte or (datetime.now() + timedelta(days=180)).strftime("%Y-%m-%d"),
expiration_date = datetime.now().strftime("%Y-%m-%d")
```

`datetime.now()` without `tz=` is on the ban list. These build date-only filter strings; in a non-UTC server timezone, "today" is ambiguous. P2 in isolation, included here for completeness.

## Why this severity

P1 — Same anti-pattern as F-0009 and F-0021, in additional locations. Mostly the same fix. The forward-fill in F-0023 (P0) is the more severe sibling; this finding catches the format/API-call violations.

## Reproduction

```
grep -nE 'datetime\.utcnow|datetime\.utcfromtimestamp|\.strftime\(["'"'"'].*Z["'"'"']' PythonDataService/app/services/dataset_service.py PythonDataService/app/research/divergence/ingest/polygon_ingest.py
grep -n 'datetime\.now\(\)' PythonDataService/app/services/polygon_client.py
```

## Suggested resolution (NOT auto-applied)

For each location, replace per the recommended idioms in `.claude/rules/numerical-rigor.md`:

- `datetime.utcfromtimestamp(ts / 1000).strftime("...Z")` → keep `int64 ms UTC` on the wire; if a string truly is needed, build it via `datetime.fromtimestamp(ts / 1000, tz=UTC).isoformat()`.
- `datetime.utcnow()` → `datetime.now(UTC).isoformat()` for the metadata field, or omit entirely (the consumer can timestamp on receipt).
- `datetime.now().strftime(...)` → `date.today()` if a date-only is genuinely intended; better, accept the date as a parameter rather than defaulting to "server today".
- `polygon_ingest.py:226` ISO-Z column → drop. Use `unix_ts` (already `int64 ms`) as the single timestamp column.

## Provenance of the finding itself

Phase 3 / cursor: same grep batch as F-0023.
