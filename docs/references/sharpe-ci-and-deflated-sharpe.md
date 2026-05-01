# Sharpe Confidence Interval & Deflated Sharpe — port attribution

## Targets

- `PythonDataService/app/research/signal/diagnostics.py` —
  `compute_sharpe_ci` (Lo 2002 confidence interval on the annualised
  Sharpe ratio).
- `PythonDataService/app/research/signal/diagnostics.py` —
  `compute_deflated_sharpe` (Bailey & López de Prado 2014 Deflated
  Sharpe Ratio).

Both surface on the Signal Engine page (verdict block + IS-grid
headline) and are documented in
`docs/signal-engine-authority.md` § 4.6 / § 4.7.

## References

* Lo, A. W. (2002). *The Statistics of Sharpe Ratios.* Financial
  Analysts Journal, 58(4), 36–52. Closed-form variance of the Sharpe
  estimator under IID returns; the autocorrelation correction we
  apply substitutes `N_eff` for the raw `T`.
* Bailey, D. H. & López de Prado, M. (2014). *The Deflated Sharpe
  Ratio: Correcting for Selection Bias, Backtest Overfitting, and
  Non-Normality.* Journal of Portfolio Management, 40(5), 94–107.
  The DSR formula and the expected-maximum-under-null benchmark.

## Math summary

### Sharpe CI (Lo 2002, eq. 14, with N_eff substitution)

Per-period (per-bar) Sharpe estimator variance under IID returns:

    Var(SR_p) = (1 + 0.5·SR_p²) / N_eff

Annualised CI:

    SE(SR_a) = sqrt(Var(SR_p)) · sqrt(B)
    CI       = SR_a ± Φ⁻¹(1 − α/2) · SE(SR_a)

with `B = 252 × 390 = 98,280` for 1-min bars.

### Deflated Sharpe (Bailey & López de Prado 2014, eq. 6 & 9)

Expected maximum Sharpe under the null of zero true Sharpe across
`N_trials` independent grid searches:

    SR_0_standardised = (1 − γ_E) · Φ⁻¹(1 − 1/N) + γ_E · Φ⁻¹(1 − 1/(N·e))
    SR_0              = SR_0_standardised / sqrt(T_eff)

with `γ_E ≈ 0.5772156649` (Euler-Mascheroni). DSR (probability the
true Sharpe exceeds zero given the observed maximum) is:

    z   = (SR_p − SR_0) · sqrt(T_eff − 1) / sqrt(1 − γ₃·SR_p + (γ₄ − 1)/4 · SR_p²)
    DSR = Φ(z)

where `γ₃` is skewness of bar returns and `γ₄` is Pearson kurtosis
(not Fisher / excess).

## Worked-example golden values

These pin the implementation against the closed-form math, not against
a reference implementation — there is no LEAN / mlfinlab equivalent
that ships with the same `N_eff` substitution. Each value below is
re-derivable by plugging the inputs into the formulas above.

### Sharpe CI

Input: 500 IID returns drawn from `N(0.001, 0.02²)` with seed = 42.

    SR_p     ≈ 0.0463
    SR_a     = SR_p · sqrt(98,280) ≈ 14.527  (illustrative; deliberately
                                              chosen so the per-period
                                              SR is small)
    SE(SR_p) = sqrt((1 + 0.5·0.0463²) / 500) ≈ 0.04473
    SE(SR_a) ≈ 14.014
    95 % CI  ≈ [SR_a − 27.47, SR_a + 27.47]

The unit test
`test_compute_sharpe_ci_brackets_point_estimate` doesn't pin these
exact values (the seed-dependent SR_a wanders), but it does enforce
the structural invariants:

* `ci_lower < point < ci_upper`
* `(ci_upper − point) ≈ (point − ci_lower)` within 1 % rel
* widening when N_eff shrinks (`test_compute_sharpe_ci_widens_when_n_eff_shrinks`)

### Deflated Sharpe — null parameters

For `N_trials = 16, T_eff = 1000`:

    SR_0_standardised ≈ (1 − γ_E)·Φ⁻¹(1 − 1/16) + γ_E·Φ⁻¹(1 − 1/(16·e))
                      ≈ 0.4228 · 1.5341 + 0.5772 · 1.7895
                      ≈ 1.6815
    SR_0_per_period   ≈ 1.6815 / sqrt(1000) ≈ 0.05317

A `selected_sharpe_annual` of 1.0 corresponds to `SR_p ≈ 0.003189`,
which is well below `SR_0` ⇒ DSR < 0.5 (the observed max is no
better than chance under the null). The test
`test_compute_deflated_sharpe_more_trials_lowers_dsr` enforces the
monotonic relationship: more trials ⇒ higher SR_0 ⇒ lower DSR.

## Tolerance rationale

The unit tests use `pytest.approx(..., abs=1e-12)` for *structural*
properties (CI symmetry, kurtosis ≈ 3 for normal samples, sum_weights
exactness) where the math reduces to deterministic floating-point
arithmetic at IEEE 754 precision. They use `abs=0.5` or wider on
empirical statistics computed from finite seeded samples
(`N_eff_assets ≈ 3.0` for orthogonal random returns, etc.) because
those depend on the seed and small-sample noise dominates inside that
band.

The CI formula itself is a closed-form combination of `np.mean`,
`np.std`, `scipy.stats.norm.ppf`, and `math.sqrt`. There are no
iterative solvers, no early termination, and no numerical
instability we expect to surface beyond the structural identities the
tests already pin. If you find a case where the CI bounds disagree
with the formula by more than `1e-9`, that is a bug worth filing.

The DSR probability via `scipy.stats.norm.cdf` saturates near 0 / 1
for extreme `z`, so equality assertions on DSR in the [0.4999..., 1)
range require looser tolerances. The current tests deliberately avoid
asserting an exact DSR value and instead enforce monotonicity in
`n_trials` (more trials ⇒ tighter null ⇒ lower DSR for a fixed
observed Sharpe), which is robust to those saturation effects.

## Open items

* No reference implementation comparison yet. The closest external
  source is `mlfinlab.backtest_statistics.deflated_sharpe_ratio` —
  add a parity test against a pinned mlfinlab version once the
  dependency is justified for this project (currently it is not).
* No bootstrap-CI variant of the Sharpe interval. Stage 2 of the
  graduation ladder calls for a block-bootstrap CI; that's a
  separate port and will live in its own `docs/references/` entry.
