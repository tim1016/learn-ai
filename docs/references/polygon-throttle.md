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

## Not a retry handler

This is a **preemptive** throttle, not a reactive one. If Polygon returns a 429
for any other reason (network weirdness, account-level slowdown), the client
will still propagate the error. Reactive retry-on-429 is a separate feature
that would live in the same class but handle the symmetric case.
