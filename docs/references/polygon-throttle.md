# Polygon client — proactive throttle

## Why it exists

Polygon's pricing plans set a hard per-minute cap on API requests:

| Plan        | Requests per minute |
|-------------|---------------------|
| Starter     | 5                   |
| Developer   | 100                 |
| Advanced    | unlimited           |
| Business    | unlimited           |

If you exceed the cap, Polygon returns `HTTP 429 Too Many Requests`. Two things
happen from there:

1. The failed request has to be retried, adding latency.
2. Polygon logs the over-cap behaviour against your account and can slow
   *all* subsequent traffic for a while.

The `PolygonClientService` doesn't wait for that to happen. It **paces**
requests on the way out — sleeps before sending — so the per-minute budget is
never exceeded in the first place.

## The layman story (what users see)

On Starter, if a Data Lab fetch needs more than 5 chunks of data, the 6th
chunk has to wait up to ~60 seconds for a request "slot" to free up.

The UI tells the user exactly this, via the Auto Chunk readout:

> Auto → 7 chunks (~42,000 bars). Your Polygon Starter plan allows 5
> requests/minute — this will take ~60s.

And the server logs a matching line at INFO level each time it paces:

    [THROTTLE] Paused 11.3s on aggs:SPY — your Polygon plan allows 5 requests/min.

## How it works

A sliding window of the last N request timestamps (where N = the plan's per-minute cap) is kept in memory, guarded by a `threading.Lock` so concurrent FastAPI workers share the same budget.

Before every Polygon request:

1. Drop any timestamps older than 60 seconds (they fell out of the window).
2. If the window is full, sleep until the oldest one falls out, then retry.
3. Otherwise append `now()` and proceed.

This is a **sliding-window rate limiter**, not a token bucket — it's simpler,
deterministic, and the sleep time directly maps to user-facing latency.

## Configuration

Set via `POLYGON_RATE_LIMIT_PER_MIN` in environment / `.env`:

- `5` — Starter plan (default)
- `100` — Developer plan
- `0` — Disable the throttle (for Advanced / Business plans, or local
  development where you've already hit the daily free-tier cap and want to
  fail fast)

## Where the throttle is applied

Currently wired into:

- `PolygonClientService.fetch_aggregates()` — the hot path for the Data Lab
  chunked fetch. One call = one Polygon request.

Not yet wired into:

- Reference endpoints (splits, dividends, news, financials) — these are
  one-shot calls used for companion files; they share the same per-minute
  budget but don't yet acquire from the throttle. TODO when the companion
  generator runs into 429s in practice.
- Options chains / trades / quotes — likewise.

Add `self._throttle.acquire(label="<endpoint>:<ticker>")` before any new
Polygon call that is likely to run in a burst.

## Not a retry handler

This is a **preemptive** throttle, not a reactive one. If Polygon returns a 429
for any other reason (network weirdness, account-level slowdown), the client
will still propagate the error. Reactive retry-on-429 is a separate feature
that would live in the same class but handle the symmetric case.

## Testing

`PythonDataService/tests/services/test_polygon_throttle.py` uses a fake clock
(`monkeypatch` on `time.monotonic` and `time.sleep`) to verify that the
throttle issues exactly the configured number of requests per minute and that
the 6th request blocks for ~60s in the Starter configuration.
