# LEAN deci-cent price encoding — port attribution

## Target
`PythonDataService/app/data_lake/lean_writer.py::to_deci_cent` — the single
canonical implementation. Every writer in the tree (the lake's own zip
builders and `app.engine.data.lean_format`'s pre-lake policy-store writers)
encodes prices through it. Consolidated by #1839.

## Reference
- **Repo**: [QuantConnect/Lean](https://github.com/QuantConnect/Lean), `master` branch, checked 2026-08-29.
- **Scale factor**: `Common/Data/Market/TradeBar.cs:36`, `private const decimal _scaleFactor = 1 / 10000m;` — the 1/10,000 grid our own `_PRICE_SCALE = Decimal(10_000)` matches.
- **Writer**: `Common/Util/LeanData.cs`, private method `Scale(decimal value)`:
  ```csharp
  private static string Scale(decimal value)
  {
      return Extensions.NormalizeToStr(value * 10_000m);
  }
  ```
  `NormalizeToStr` (`Common/Extensions.cs`) is `Normalize(input).ToString(...)`,
  and `Normalize` is `input / 1.000000000000000000000000000000000m` — a
  trailing-zero-stripping identity operation. **There is no `Math.Round`,
  `decimal.Round`, integer cast, or any other rounding/truncation call
  anywhere in LEAN's write path.**

## What this means for our port

LEAN does not quantize to an integer at write time. For a price that
already lands exactly on the 1/10,000 grid (true for the overwhelming
majority of Polygon-sourced data, which reports at most 4 decimal places),
`Scale` produces an integer-valued string and our `int(...)` output is
byte-identical to LEAN's. For a price finer than the grid (a sub-$1.00 name
under Reg NMS's $0.0001 tick, or a provider revision carrying more
precision than the tape), LEAN would write the literal fractional scaled
value (e.g. `1234567.5`) — not round it to an integer at all.

Our on-disk format requires an integer field (see the module docstring's
CSV layout), so encoding a sub-grid price is **our own quantization
decision**, not a literal replication of a LEAN tie-breaking rule — LEAN
has none to replicate. Among the choices available for that quantization,
`ROUND_HALF_UP` was picked over truncation (`int(price * 10_000)`, the
pre-#1839 behavior of `app.engine.data.lean_format`) because truncation is
a systematic downward bias (expected error `-0.5` deci-cent on every OHLC
field) where half-up is symmetric (expected error `0`). This is an
engineering argument for internal consistency and unbiased quantization,
not a claim of proven equivalence to LEAN's literal write path — which,
for this specific input class, would not itself produce an integer.

## Tolerance
Not a strict-float-equivalence port in the numerical-rigor.md sense — there
is no LEAN reference *rounding rule* to match, because LEAN doesn't rescale
to an integer. The invariant actually under test is internal: both of our
writers (`app.data_lake.lean_writer` and `app.engine.data.lean_format`)
agree bar-for-bar, on prices at and finer than the deci-cent grid, by
construction rather than by chance.

## Tests
`PythonDataService/tests/unit/data_lake/test_deci_cent_canonical.py` pins
the `ROUND_HALF_UP` rule with hand-computed cases and asserts both writers
agree row-for-row on sub-grid prices.
`PythonDataService/tests/unit/data_lake/test_lean_writer.py::test_to_deci_cent_rounds_half_up`
covers the same rule directly on `to_deci_cent`.

## Open items
- If a real LEAN artifact containing a genuinely sub-grid price (5+
  decimal places) is ever captured from a live feed, it would be worth
  fixturing to confirm LEAN's downstream `LineParseScale`/`StreamParseScale`
  readers round-trip a non-integer scaled string correctly — our own
  readers only ever need to parse integers we wrote ourselves, so this
  divergence is currently untested on the read side and believed to be
  unreachable in practice (Polygon does not emit 5-decimal prices), not
  proven unreachable.
