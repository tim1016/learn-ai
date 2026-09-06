# Cross-stack contract fixtures

These JSON documents are intentional wire examples shared by tests at the
FastAPI, .NET, and Angular boundaries. They pin field names, nullability, and
the `int64 ms UTC` timestamp convention; they are not golden trading results.

- `aggregate-response-v1.json` is the Python aggregate-bars response consumed
  by the .NET `PolygonService`.
- `spec-strategy-backtest-response-v1.json` is the Python backtest response
  the Angular spec-strategy runner consumes directly (its .NET bridge was
  retired in #1963).
- `data-plane-health-v1.json` is a direct FastAPI-to-Angular control-plane
  response and deliberately has no .NET hop.
