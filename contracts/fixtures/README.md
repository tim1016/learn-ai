# Cross-stack contract fixtures

These JSON documents are intentional wire examples shared by tests at the
FastAPI, .NET, and Angular boundaries. They pin field names, nullability, and
the `int64 ms UTC` timestamp convention; they are not golden trading results.

- `aggregate-response-v1.json` is the Python aggregate-bars response consumed
  by the .NET `PolygonService`.
- `spec-strategy-backtest-response-v1.json` is the Python backtest response
  the Angular spec-strategy runner consumes directly (its .NET bridge was
  retired in #1963).
- `golden-search-importance-order-v1.json` pins the Golden Search search order
  (ADR 0074 decision 10) that Python's `protocol.by_importance` freezes and
  Angular's `byImportance` shows, so the two cannot drift apart.
