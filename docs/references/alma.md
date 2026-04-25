# Arnaud Legoux Moving Average — port attribution

## Target
`PythonDataService/app/services/dataset_service.py` — Data Lab dispatch
calls `pandas_ta.alma(close, length=...)`. No in-repo port.

## Reference
- **Library**: pandas-ta v0.4.71b0, function `alma`
- **Math source**: Arnaud Legoux & Dimitrios Kouzis-Loukas, *Arnaud
  Legoux Moving Average* (2009). Gaussian-weighted, offset-shifted
  moving average.

## Math summary
With window length `N`, sigma `σ`, and `dist_offset ∈ [0, 1]`:

```
m   = dist_offset × (N − 1)
s   = N / σ
w_i = exp(−(i − m)² / (2 × s²))     for i = 0, 1, ..., N−1
ALMA_t = sum(w_i × close_{t−N+1+i}) / sum(w_i)
```

The weight peak sits at index `m`. With `dist_offset=0.85`, the peak is
shifted toward recent bars (~85 % of the way along the window) — high
responsiveness with a smoothing tail. Lower offsets shift the peak
toward older bars (smoother, more lagged). `σ` controls the bell width:
larger `σ` → sharper peak (more weight on the peak bar), smaller `σ` →
flatter weights (closer to a SMA).

The first `N−1` bars are `NaN`.

## Parameters exposed in Data Lab
| Param | Type | Default | Range |
|-------|------|---------|-------|
| `length` | int | **10** | [1, 500] |

`sigma` and `dist_offset` are **not exposed** in the Data Lab UI;
pandas-ta's defaults flow through:
- `sigma = 6.0`
- `dist_offset = 0.85`

⚠️ **Default divergence on length**: pandas-ta's own default is
`length=9`. The Data Lab UI defaults to `length=10`. Intentional UX
choice.

## Output
Single Series. pandas-ta names it `ALMA_{N}_{sigma}_{dist_offset}`;
the Data Lab dispatch lowercases to `alma_length{N}` because the
display column name is keyed off the *exposed* params, not pandas-ta's
internal naming. The numeric values are identical.

## Tolerance
Transport pass-through; equivalence is *by reference* to pandas-ta
v0.4.71b0. Because pandas-ta's `dist_offset` and `sigma` defaults
materially affect the curve shape, any future library upgrade that
changes those defaults must be treated as a math change, not a refactor.

## Tests
None specific. Covered by Data Lab dataset-generation integration tests.

## Open items
- No golden fixture.
- `sigma` and `dist_offset` are not exposed in the UI; expose them if
  users start asking for tunable smoothness/responsiveness trade-offs.
