# RV-003 — IV-RV Basis Conversion (ACT/365 → TRD/252)

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 3 cases with varying σ,
tenor, and asof date. NYSE trading-day count (N) from `pandas_market_calendars`
— pinned in fixture as `n_trading_pinned` for audit transparency.

**Layer 2 — Methodology provenance:** Practitioner convention: variance accrues
only on trading days. Formula per `docs/references/iv-rv-basis-alignment.md`.
Canonical: `app/volatility/basis.py::convert_iv_act365_to_trading252`.

**Layer 3 — Independent numerical oracle:** Direct formula application using
`pandas_market_calendars` for N (same library as canonical) but applying the
formula without calling the canonical function.

## Formula

```text
σ²_TRD252 · (N/252) = σ²_ACT365 · (D/365)   ← equate total variance

σ_TRD252 = σ_ACT365 · √((D · 252) / (365 · N))

D = tenor_calendar_days
N = NYSE trading sessions in [asof_date, asof_date + D)
```

## Pinned Cases

| σ_ACT365 | D (cal days) | asof       | N  | σ_TRD252 (oracle) |
|----------|--------------|------------|----|-------------------|
| 0.20     | 30        | 2024-01-02 | 20  | 0.203530      |
| 0.30     | 21        | 2024-01-02 | 13  | 0.316820      |
| 0.25     | 30        | 2024-04-01 | 21  | 0.248282      |

N is the NYSE trading-day count from `pandas_market_calendars` at generation
time and is stored in the fixture for traceability.

## Canonical Implementation

`PythonDataService/app/volatility/basis.py::convert_iv_act365_to_trading252`

## Tolerance

atol=1e-9, rtol=0.0. Rationale: the oracle and canonical apply identical
single-multiplication arithmetic (σ × √factor); the float64 rounding is
deterministic and agrees to < 1e-16.

## Regeneration

```bash
python scripts/generate_fixtures.py --id RV-003 --force \
  --justification "<reason>"
```

## Generation Metadata

Generated: 2026-05-08
Oracle: literature_formula — σ_TRD252=σ_ACT365·√(D·252/(365·N)) with N from pandas_market_calendars
Script: scripts/fixture_generators/volatility.py
(initial generation)
