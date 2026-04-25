# Polygon client — proactive throttle

## Why it exists

Only Polygon's **free Basic tier** caps requests at 5/minute. **All paid plans
(Starter, Developer, Advanced, Business) have no per-minute cap.** Verify the
current limit on your account at [polygon.io/pricing](https://polygon.io/pricing)
since plan terms change.

| Plan        | Per-minute cap |
|-------------|----------------|
| Basic (free)| 5              |
| Starter     | unlimited      |
| Developer   | unlimited      |
| Advanced    | unlimited      |
| Business    | unlimited      |

If you exceed the cap on the free tier, Polygon returns `HTTP 429 Too Many
Requests`. Two things happen from there:

1. The failed request has to be retried, adding latency.
2. Polygon logs the over-cap behaviour against your account and can slow
   *all* subsequent traffic for a while.

The `PolygonClientService` *can* pace requests on the way out — sleeping
before sending — so the per-minute budget is never exceeded. By default this
is **off** (`POLYGON_RATE_LIMIT_PER_MIN=0`) since the codebase assumes a paid
plan; flip it on for the free tier.

## The layman story (what users see)

When the throttle is on (free tier), if a Data Lab fetch needs more than the
plan's per-minute allowance, later requests pause for a "slot" to free up. On
paid plans the throttle is off entirely and requests issue back-to-back.

The UI tells the user via the Auto Chunk readout (only meaningful when the
throttle is active):

> Auto → 7 chunks (~42,000 bars). Your Polygon plan allows 5
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

- `0` — Throttle off (default). Use this on any paid Polygon plan
  (Starter / Developer / Advanced / Business) — they have no per-minute
  cap and the throttle would only add artificial latency.
- `5` — Free Basic tier. Paces fetch requests so they never exceed
  Polygon's 5/min cap and never trigger a 429.
- Any positive integer — for plan tiers that publish a different cap,
  or for self-imposed rate limits during testing.

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
