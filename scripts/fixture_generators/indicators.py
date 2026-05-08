"""Generators for IND-001 (EMA), IND-002 (SMA), IND-003 (RSI) golden fixtures.

Oracle: hand_computed — formula applied to small seeded synthetic series without
calling the canonical indicator classes. LEAN C# cannot be run from Python;
pandas-ta would be internal_regression. Hand-computing an 8-element series is
auditable and qualifies as external certification tier.

All indicators use period=3.

IND-001 — EMA(period=3)
  k = 2/(1+3) = 0.5
  Warmup (samples 1-3): SMA-seeded — EMA = mean of all samples so far
  Post-warmup (samples 4+): EMA[n] = price[n]*0.5 + EMA[n-1]*0.5
  is_ready at samples >= 3

IND-002 — SMA(period=3)
  SMA[n] = mean of the last 3 prices (or all prices if < 3 seen)
  is_ready at samples >= 3

IND-003 — RSI(period=3), Wilder's smoothing
  Seed: simple mean of first 3 gain/loss deltas (samples 2-4)
  Post-warmup Wilder: avg_new = (avg_old*(period-1) + sample) / period
  is_ready at samples >= period+1 = 4

Input: 3 test cases, each with 8 price bars (columns p0..p7).
Output: 8 indicator values per bar (v0..v7). NaN for bars where
        _compute_next_value returned None (RSI before is_ready).
        EMA and SMA always return a value (warmup = running SMA).

Prices chosen to produce non-trivial, hand-verifiable values:
  Case A: [10, 12, 14, 16, 18, 20, 22, 24]  — monotone increasing
  Case B: [50, 52, 50, 54, 52, 56, 54, 58]  — alternating up/down
  Case C: [100, 95, 105, 100, 110, 105, 115, 110] — volatile with reversals
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pyarrow as pa

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "PythonDataService" / "tests" / "fixtures"))
sys.path.insert(0, str(REPO_ROOT / "PythonDataService"))

from golden_support.hashing import compute_hashes  # noqa: E402
from golden_support.io import write_arrow  # noqa: E402

def _generation_date() -> str:
    return date.today().isoformat()
PERIOD = 3

PRICE_CASES: list[list[float]] = [
    [10.0, 12.0, 14.0, 16.0, 18.0, 20.0, 22.0, 24.0],
    [50.0, 52.0, 50.0, 54.0, 52.0, 56.0, 54.0, 58.0],
    [100.0, 95.0, 105.0, 100.0, 110.0, 105.0, 115.0, 110.0],
]


# ── Hand-computed oracles ─────────────────────────────────────────────────────


def _oracle_ema(prices: list[float], period: int) -> list[float]:
    """EMA with SMA-seeded warmup; k = 2/(1+period).

    During warmup (samples 1..period): returns running arithmetic mean.
    Post-warmup: standard EMA recursion.
    Always returns a value (never None).
    """
    k = 2.0 / (1 + period)
    one_minus_k = 1.0 - k
    result: list[float] = []
    ema: float | None = None
    warmup_sum = 0.0

    for i, p in enumerate(prices):
        sample = i + 1  # 1-based sample count
        warmup_sum += p
        if sample <= period:
            # SMA warmup: running mean of all samples seen so far
            ema = warmup_sum / sample
        else:
            assert ema is not None
            ema = p * k + ema * one_minus_k
        result.append(ema)

    return result


def _oracle_sma(prices: list[float], period: int) -> list[float]:
    """SMA with rolling window; during warmup returns mean of all samples seen.

    Matches LEAN's SMA which uses a deque of maxlen=period and returns
    _sum / len(window) (not _sum / period) during warmup.
    Always returns a value.
    """
    result: list[float] = []
    window: list[float] = []
    for p in prices:
        if len(window) == period:
            window.pop(0)
        window.append(p)
        result.append(sum(window) / len(window))
    return result


def _oracle_rsi(prices: list[float], period: int) -> list[float | None]:
    """RSI with Wilder's smoothing.

    Matches canonical rsi.py exactly:
      - First sample: no delta → None
      - Samples 2..(period+1): accumulate gain/loss for initial SMA seed → None
      - Sample period+1: seed avg_gain, avg_loss from simple mean → return RSI
      - Subsequent: Wilder smoothing (avg*(period-1) + sample) / period

    Edge case: if round(avg_loss, 10) == 0, RSI = 100.
    is_ready at samples >= period+1.
    """
    result: list[float | None] = []
    prev: float | None = None
    avg_gain: float | None = None
    avg_loss: float | None = None
    gain_sum = 0.0
    loss_sum = 0.0
    delta_samples = 0

    for p in prices:
        if prev is None:
            prev = p
            result.append(None)
            continue

        gain = max(0.0, p - prev)
        loss = max(0.0, prev - p)
        prev = p
        delta_samples += 1

        if delta_samples < period:
            gain_sum += gain
            loss_sum += loss
            result.append(None)
        elif delta_samples == period:
            gain_sum += gain
            loss_sum += loss
            avg_gain = gain_sum / period
            avg_loss = loss_sum / period
            if round(avg_loss, 10) == 0.0:
                result.append(100.0)
            else:
                rs = avg_gain / avg_loss
                result.append(100.0 - 100.0 / (1.0 + rs))
        else:
            assert avg_gain is not None and avg_loss is not None
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period
            if round(avg_loss, 10) == 0.0:
                result.append(100.0)
            else:
                rs = avg_gain / avg_loss
                result.append(100.0 - 100.0 / (1.0 + rs))

    return result


# ── Table builders ────────────────────────────────────────────────────────────


def _build_input_table() -> pa.Table:
    n_bars = len(PRICE_CASES[0])
    return pa.table(
        {f"p{i}": pa.array([c[i] for c in PRICE_CASES], type=pa.float64()) for i in range(n_bars)}
    )


def _build_output_table(outputs: list[list[float | None]]) -> pa.Table:
    n_bars = len(outputs[0])
    return pa.table(
        {
            f"v{i}": pa.array(
                [row[i] if row[i] is not None else float("nan") for row in outputs],
                type=pa.float64(),
            )
            for i in range(n_bars)
        }
    )


# ── Write helpers ─────────────────────────────────────────────────────────────


def _write_and_report(
    version_dir: Path,
    fixture_id: str,
    input_table: pa.Table,
    output_table: pa.Table,
    write_attribution_fn,
    justification: str,
) -> None:
    input_path = version_dir / "input.arrow"
    output_path = version_dir / "output.arrow"
    attribution_path = version_dir / "attribution.md"

    write_arrow(input_table, input_path)
    write_arrow(output_table, output_path)
    write_attribution_fn(attribution_path, justification)

    content_hashes, file_hashes = compute_hashes(version_dir, ["input.arrow", "output.arrow"])

    print(f"  {fixture_id}: {len(input_table)} cases × {len(PRICE_CASES[0])} bars")
    print(f"  content_sha256[input.arrow]:  {content_hashes['input.arrow']}")
    print(f"  content_sha256[output.arrow]: {content_hashes['output.arrow']}")
    print(f"  file_sha256[input.arrow]:     {file_hashes['input.arrow']}")
    print(f"  file_sha256[output.arrow]:    {file_hashes['output.arrow']}")
    print()
    print("  Paste into manifest.json versions entry:")
    print(
        f"""  {{
    "input": "input.arrow",
    "output": "output.arrow",
    "attribution": "attribution.md",
    "content_sha256": {{
      "input.arrow": "{content_hashes['input.arrow']}",
      "output.arrow": "{content_hashes['output.arrow']}"
    }},
    "file_sha256": {{
      "input.arrow": "{file_hashes['input.arrow']}",
      "output.arrow": "{file_hashes['output.arrow']}"
    }}
  }}"""
    )


# ── IND-001: EMA ──────────────────────────────────────────────────────────────


def _write_attribution_ind001(path: Path, justification: str) -> None:
    k = 2.0 / (1 + PERIOD)
    # Hand-verify Case A for the doc
    a = PRICE_CASES[0]
    sma3 = (a[0] + a[1] + a[2]) / 3
    ema4 = a[3] * k + sma3 * (1 - k)
    path.write_text(
        f"""# IND-001 — Exponential Moving Average (period={PERIOD})

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 3-case grid of 8-bar price
series spanning monotone, alternating, and volatile patterns.
See `indicators.py::PRICE_CASES`.

**Layer 2 — Methodology provenance:** LEAN `Indicators/ExponentialMovingAverage.cs`
(vendored at `references/lean/7986ed0aade3ae5de06121682409f05984e32ff7`).
SMA-seeded warmup; k = 2/(1+period).

**Layer 3 — Independent numerical oracle:** Pure-Python formula without calling
the canonical `ExponentialMovingAverage` class. Seeding and recursion written
from first principles.

## Formula

k = 2/(1+{PERIOD}) = {k}
Warmup (samples 1..{PERIOD}): EMA = mean(prices[0..i])  — running arithmetic mean
Post-warmup:                 EMA[n] = price[n]*{k} + EMA[n-1]*{1-k}

## Critical Seeding Detail

The EMA seeds with the running arithmetic mean during warmup, not NaN.
At samples={PERIOD}, EMA = SMA(all {PERIOD} warmup prices) — this is the canonical seed.
Post-warmup starts at sample {PERIOD+1}.

## Hand-Verification (Case A: prices={PRICE_CASES[0][:4]})

sample 1: EMA = {PRICE_CASES[0][0]}   (mean of [p0])
sample 2: EMA = {(PRICE_CASES[0][0]+PRICE_CASES[0][1])/2}  (mean of [p0,p1])
sample 3: EMA = {sma3}  (SMA seed = mean of [p0,p1,p2])
sample 4: EMA = {a[3]}*{k} + {sma3}*{1-k} = {ema4}

## Canonical Implementation

`PythonDataService/app/engine/indicators/ema.py::ExponentialMovingAverage`
`_compute_next_value` uses an embedded `SimpleMovingAverage` for warmup.

## Tolerance

atol=1e-9, rtol=0.0

## NaN Convention

v0..v7 in the output table contain float values for all bars (no NaN),
because the EMA always returns a value (warmup returns the running SMA).

## Regeneration

  python scripts/generate_fixtures.py --id IND-001 --force \\
    --justification "<reason>"

## Generation Metadata

Generated: {_generation_date()}
Oracle: hand_computed — pure-Python SMA-seeded EMA formula without calling canonical
Script: scripts/fixture_generators/indicators.py
{'Justification: ' + justification if justification else '(initial generation)'}
""",
        encoding="utf-8",
    )


def generate_ind001(version_dir: Path, justification: str = "") -> None:
    """Generate IND-001: EMA(period=3) fixture."""
    outputs = [_oracle_ema(prices, PERIOD) for prices in PRICE_CASES]
    _write_and_report(
        version_dir,
        "IND-001",
        _build_input_table(),
        _build_output_table(outputs),
        _write_attribution_ind001,
        justification,
    )


# ── IND-002: SMA ──────────────────────────────────────────────────────────────


def _write_attribution_ind002(path: Path, justification: str) -> None:
    a = PRICE_CASES[0]
    path.write_text(
        f"""# IND-002 — Simple Moving Average (period={PERIOD})

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. Same 3-case grid as IND-001.
See `indicators.py::PRICE_CASES`.

**Layer 2 — Methodology provenance:** LEAN `Indicators/SimpleMovingAverage.cs`
(vendored at `references/lean/7986ed0aade3ae5de06121682409f05984e32ff7`).
Rolling window of maxlen=period; during warmup returns mean of all samples seen.

**Layer 3 — Independent numerical oracle:** Pure-Python list-based rolling window
without calling `SimpleMovingAverage` class.

## Formula

Warmup (samples 1..{PERIOD-1}): SMA = mean(prices[0..i])  — growing window
Post-warmup (samples {PERIOD}+): SMA = mean(prices[i-{PERIOD}+1..i])  — fixed window

## Hand-Verification (Case A: prices={a[:4]})

sample 1: SMA = {a[0]}
sample 2: SMA = {(a[0]+a[1])/2}
sample 3: SMA = {(a[0]+a[1]+a[2])/3}  (is_ready, full window)
sample 4: SMA = {(a[1]+a[2]+a[3])/3}

## Canonical Implementation

`PythonDataService/app/engine/indicators/sma.py::SimpleMovingAverage`

## Tolerance

atol=1e-9, rtol=0.0

## NaN Convention

All bars produce a value (warmup is rolling mean, not NaN).

## Regeneration

  python scripts/generate_fixtures.py --id IND-002 --force \\
    --justification "<reason>"

## Generation Metadata

Generated: {_generation_date()}
Oracle: hand_computed — pure-Python rolling window without calling canonical
Script: scripts/fixture_generators/indicators.py
{'Justification: ' + justification if justification else '(initial generation)'}
""",
        encoding="utf-8",
    )


def generate_ind002(version_dir: Path, justification: str = "") -> None:
    """Generate IND-002: SMA(period=3) fixture."""
    outputs = [_oracle_sma(prices, PERIOD) for prices in PRICE_CASES]
    _write_and_report(
        version_dir,
        "IND-002",
        _build_input_table(),
        _build_output_table(outputs),
        _write_attribution_ind002,
        justification,
    )


# ── IND-003: RSI ──────────────────────────────────────────────────────────────


def _write_attribution_ind003(path: Path, justification: str) -> None:
    b = PRICE_CASES[1]
    # Hand trace Case B first 5 samples
    deltas_b = [(b[i] - b[i - 1]) for i in range(1, 5)]
    gains_b = [max(0.0, d) for d in deltas_b]
    losses_b = [max(0.0, -d) for d in deltas_b]
    seed_avg_gain = sum(gains_b[:PERIOD]) / PERIOD
    seed_avg_loss = sum(losses_b[:PERIOD]) / PERIOD
    if round(seed_avg_loss, 10) == 0.0:
        seed_rsi: float | str = 100.0
    else:
        seed_rsi = 100.0 - 100.0 / (1.0 + seed_avg_gain / seed_avg_loss)

    path.write_text(
        f"""# IND-003 — Relative Strength Index (period={PERIOD}, Wilder's smoothing)

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. Same 3-case grid as IND-001.
See `indicators.py::PRICE_CASES`.

**Layer 2 — Methodology provenance:** LEAN `Indicators/RelativeStrengthIndex.cs`
with `MovingAverageType.Wilders`
(vendored at `references/lean/7986ed0aade3ae5de06121682409f05984e32ff7`).
Seed: simple mean of first period gain/loss deltas; then Wilder's smoothing.

**Layer 3 — Independent numerical oracle:** Pure-Python Wilder RSI formula without
calling `RelativeStrengthIndex` class.

## Formula

gain[i] = max(0, price[i] - price[i-1])
loss[i] = max(0, price[i-1] - price[i])

Seed (after period={PERIOD} deltas):
  avg_gain = mean(gain[0..{PERIOD-1}])
  avg_loss = mean(loss[0..{PERIOD-1}])

Wilder smoothing (post-seed):
  avg_gain = (avg_gain * {PERIOD-1} + gain) / {PERIOD}
  avg_loss = (avg_loss * {PERIOD-1} + loss) / {PERIOD}

RSI = 100 - 100/(1 + avg_gain/avg_loss)
Edge: if round(avg_loss, 10) == 0, RSI = 100

is_ready at samples >= {PERIOD+1} (one extra for first delta)

## NaN Convention

v0 (first sample — no delta) and v1..v{PERIOD-1} (accumulating for seed) are NaN.
First non-NaN value appears at v{PERIOD} (sample {PERIOD+1}).

## Hand-Verification (Case B, first 5 bars: {b[:5]})

deltas (gains): {gains_b[:3]}
deltas (losses): {losses_b[:3]}
Seed avg_gain={seed_avg_gain:.6f}, avg_loss={seed_avg_loss:.6f}
RSI at v{PERIOD} ≈ {seed_rsi if isinstance(seed_rsi, str) else f'{seed_rsi:.6f}'}

## Critical Seeding Convention (D-009)

First avg is simple mean of period deltas (not Wilder's of period+1).
This matches LEAN's seeding. Some textbooks differ.

## Canonical Implementation

`PythonDataService/app/engine/indicators/rsi.py::RelativeStrengthIndex`

## Tolerance

atol=1e-9, rtol=0.0

## Regeneration

  python scripts/generate_fixtures.py --id IND-003 --force \\
    --justification "<reason>"

## Generation Metadata

Generated: {_generation_date()}
Oracle: hand_computed — pure-Python Wilder RSI without calling canonical
Script: scripts/fixture_generators/indicators.py
{'Justification: ' + justification if justification else '(initial generation)'}
""",
        encoding="utf-8",
    )


def generate_ind003(version_dir: Path, justification: str = "") -> None:
    """Generate IND-003: RSI(period=3) fixture."""
    outputs: list[list[float | None]] = [_oracle_rsi(prices, PERIOD) for prices in PRICE_CASES]
    _write_and_report(
        version_dir,
        "IND-003",
        _build_input_table(),
        _build_output_table(outputs),
        _write_attribution_ind003,
        justification,
    )
