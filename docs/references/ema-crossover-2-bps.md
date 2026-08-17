# EMA Crossover 2 bps

## What changed

`EMA Crossover 2 bps` is a separately selectable, parameterized Strategy Lab
strategy. It starts from the existing `EMA Crossover Signal` and replaces its
fixed entry gates with three configurable values:

```text
original: EMA(5) - EMA(10) >= $0.20, with 50 <= RSI(14) <= 70
new:      10,000 * (EMA(5) - EMA(10)) / EMA(10) >= gap_bps
          rsi_min <= RSI(14) <= rsi_max
```

The defaults remain `gap_bps=2`, `rsi_min=50`, and `rsi_max=70`. Two basis
points means `0.02%`. For example, when EMA(10) is `$500`, the
minimum absolute gap is `$0.10`; when EMA(10) is `$250`, it is `$0.05`.

Everything else is shared with the original strategy: SPY is the default
signal stream, minute bars consolidate to 15 minutes, EMA periods are 5 and
10, RSI is Wilders 14, entry requires a fresh up-cross, the strategy is
long-only, and exit occurs after five consolidated bars. Sizing, fill mode,
fees, warmup, timestamps, diagnostics, and end-of-run liquidation are
unchanged. The original `EMA Crossover Signal` retains its fixed `$0.20` and
`50–70` rules; configuration belongs only to `EMA Crossover 2 bps`.

## Implementation design

- Python: `app/engine/strategy/algorithms/ema_crossover_2_bps.py` subclasses
  `EmaCrossoverSignalAlgorithm` and overrides the gap predicate and RSI-band
  accessor. The base class still owns the complete indicator and trade
  lifecycle.
- Boundary validation: `app/engine/strategy/registry.py::EmaCrossover2BpsParams`
  accepts finite `gap_bps` from 0 through 100 and finite RSI values from 0
  through 100, with `rsi_min < rsi_max`. The RSI boundaries are inclusive at
  signal time.
- Normalized arithmetic: `app/engine/strategy/spec/primitives.py::difference_bps`
  is the single Python authority and uses exact `Decimal` arithmetic.
- Declarative receipt: `app/engine/strategy/spec/fixtures/ema_crossover_2_bps.spec.json`.
  Its parity test proves the default 2/50/70 configuration and hand-coded
  strategy produce the same trades. It is a default receipt, not a claim that
  every custom configuration is encoded in a separate fixture.
- LEAN: `app/lean_sidecar/trusted_samples/ema_crossover_2_bps.py` derives its
  source from the original trusted EMA template with fail-closed,
  one-occurrence substitutions. It reads the same three validated values from
  LEAN runtime parameters.
- Persistence: the LEAN config and manifest record the three values, and the
  saved Strategy Lab row keeps them in `Parameters`. Restoring a LEAN run
  therefore rehydrates the configuration that actually produced it.
- Paired execution: the parity dispatcher copies the Python engine's resolved,
  schema-validated values into the automatically launched LEAN companion. It
  fails the companion instead of silently substituting defaults if a declared
  LEAN parameter is missing.
- Strategy Lab: the engine registry exposes display name `EMA Crossover 2 bps`,
  default symbol `SPY`, the three gate controls, 15-minute cadence, and LEAN
  twin `ema_crossover_2_bps`.

## Using the controls in Strategy Lab

1. Select `EMA Crossover 2 bps`.
2. Open `Advanced` → `Strategy parameters`.
3. Set `Crossover gap (bps)`, `RSI lower gate`, and `RSI upper gate`.
4. Choose `Python`, `LEAN`, or `Both`, then run validation.

`Both` is the recommended validation mode after changing a gate: the Python
run is the platform result and the aligned LEAN run is the independent parity
check. Raising `gap_bps` demands stronger EMA separation and normally reduces
eligible entries. Narrowing the RSI band also normally reduces entries. These
are hypotheses, not monotonic guarantees, because changing one entry can
alter position state and suppress a later crossover.

The settings change signal selection, not risk management. They do not change
the five-bar holding period, position sizing, fill model, commission model, or
data window. Compare custom configurations with out-of-sample or walk-forward
evidence; do not pick them solely from the best full-history return.

## Live paired Strategy Lab receipts

Executed on 2026-08-16 with Strategy Lab engine choice `Both`, on the same
window and immutable fixture:

| Field | Receipt |
|---|---|
| Symbol | SPY |
| Window | 2026-02-17 through 2026-08-14 |
| Input / signal bars | 1 minute / 15 minutes |
| Shared data fixture | `bar-store-v1-a62793ad9956ffa3` |

| Configuration | Python / LEAN runs | Trades | Net P&L | Compatibility |
|---|---|---:|---:|---|
| Defaults: 2 bps, RSI 50–70 | 146 / 147 | 38 / 38 | -$1,797.53 / -$1,797.53 | **Agree** |
| Custom: 4 bps, RSI 52–68 | 151 / 152 | 18 / 18 | -$1,689.17 / -$1,689.17 | **Agree** |

The compatibility evidence reports matching shared input, matching
LEAN-native values, and matching readiness fields. The custom receipt also
proves that non-default gates reach both engines and survive persistence. These
receipts establish implementation agreement for this pinned window and data
fixture; they do not establish that the strategy has a positive edge. Both
configurations produced negative P&L on this window.

## Running LEAN locally

From the repository root, start the host-side LEAN launcher with:

```bash
cd PythonDataService
.venv/bin/python -m uvicorn app.lean_sidecar.launcher.app:app --host 0.0.0.0 --port 8090
```

Then open Strategy Lab, choose `EMA Crossover 2 bps`, select `LEAN` or `Both`,
and run validation. The launcher must bind `0.0.0.0`; the data-service
container reaches it through `host.containers.internal:8090`.

## Validation boundaries

- Formula golden fixture: exact `Decimal` equality (`atol=0`, `rtol=0`).
- StrategySpec versus Python strategy: trade-by-trade exact parity on the
  controlled synthetic stream.
- LEAN source: parseability, pinned non-gate constants, runtime parameter reads,
  and fail-closed audited source derivation.
- API boundaries: finite/bounded gate values, strict RSI ordering, unknown-key
  rejection, and rejection on templates that do not declare these parameters.
- Cross-engine runtime: normal Strategy Lab persistence and compatibility
  verdict, not an ad-hoc calculation.

This is research software, not financial advice. A green compatibility
receipt proves implementation agreement, not future profitability.
