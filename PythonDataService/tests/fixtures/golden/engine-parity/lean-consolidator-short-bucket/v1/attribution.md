# lean-consolidator-short-bucket — LEAN's own answer for a bucket missing its last minute

**Derived:** 2026-10-10, for #2909.
**Purpose:** Pin, from LEAN itself, what a time-based `TradeBarConsolidator` emits when a
15-minute bucket's final minute never arrives: LEAN's bar ends at **start + period** and is
evaluated, priced at the close of the last minute that did arrive. The Python port
(`app/engine/consolidators/trade_bar_consolidator.py`) must emit byte-identical fields.

## How it was derived (the audit trail)

The harness beside this entry
(`scripts/fixture_generators/lean_consolidator_short_bucket/harness.cs`) instantiates
**LEAN's own** `TradeBarConsolidator(TimeSpan.FromMinutes(15))`, feeds it the fifteen input
minutes of `input.json` (fourteen inside the 10:00–10:15 bucket — the 10:14 minute never
arrives — then the 10:16 bar that fires it), and prints every input bar and the emitted
consolidated bar. It was executed in the repo's pinned LEAN sandbox image
(`quantconnect/lean@sha256:4934c22c2b080a688f25b571746603e01533c5e581499d8457e5624a132ba77b`,
mounted through `localhost/learn-ai/lean-sandbox:arm64-dotnet109`):

```
podman run --rm --entrypoint sh \
  -v <harness-dir>:/harness-ro:ro \
  localhost/learn-ai/lean-sandbox:arm64-dotnet109 \
  -c "cp -r /harness-ro /tmp/h && cd /tmp/h && dotnet run"
```

The captured output — one emitted bar — is `output.json`, verbatim:
`Time=10:00 EndTime=10:15 O=500.4 H=500.4 L=496.4 C=496.4 V=1491`.

## Cross-check against the pinned vendored commit

The sandbox image is pinned by digest, not git commit. The repo's pinned LEAN source commit
is `7986ed0aade3ae5de06121682409f05984e32ff7` (vendored under `references/lean/`), and its
sources read exactly as the run behaved — they are the audit anchor:

- `Common/Data/Market/TradeBar.cs`: `EndTime { get { return Time + Period; } ... }` — the
  emitted bar's end is **computed**, never the last contained minute's end.
- `Common/Data/Consolidators/TradeBarConsolidator.cs`: `AggregateBar` writes `Time =
  GetRoundedBarTime(data)` and `Period = (TimeSpan)Period` once, when the working bar
  opens, and never rewrites either; only OHLCV merge on later inputs.

## Interpretation notes

- LEAN's consolidator works on naive `DateTime`s that its engine has already resolved to
  exchange-local wall time; the harness feeds New York wall time directly, which is exactly
  what the port's `_floor_to_period_ms` derives from `int64 ms UTC`. 2026-03-18 is EDT, so
  `10:00` here is `14:00Z`.
- `close` is the 10:13 minute's close (the last that arrived), `volume` is the sum of the
  fourteen arrived minutes (1491), `open`/`high`/`low` fold the flat per-minute bars.

## Regeneration

Re-run the harness command above with a fresh copy of `harness.cs`/`harness.csproj`; the
printed `INPUT`/`EMITTED` lines are this fixture's `input.json`/`output.json`. Then
`PYTHONPATH=. python scripts/fixture_generators/lean_consolidator_short_bucket.py` to
re-pin the manifest hashes.
