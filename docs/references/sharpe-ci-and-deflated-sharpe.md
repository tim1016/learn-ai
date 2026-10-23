# Sharpe inference — source facts

## Lo (2002)

[Andrew W. Lo, *The Statistics of Sharpe Ratios*, Financial Analysts Journal 58(4), pp. 36–52](https://traders.studentorg.berkeley.edu/papers/The-Statistics-of-Sharpe-Ratios.pdf).

- Eq. 3 defines variance with denominator T.
- Eq. 9–10 give the normal-IID Sharpe standard error and normal interval.
- Table 1 gives SE=0.188 at SR=1.5, T=60; SE=0.303 at SR=3, T=60.
- Eq. 14–15 give the general delta-method variance `g′Ωg/T`. The moment vector is `(r−μ, (r−μ)²−v)` and gradient is `(1/√v, −μ/(2v^(3/2)))`. The Appendix explains covariance estimation under serial dependence.
- Square-root annualization under serial correlation does not generally equal the Sharpe of annual aggregated returns.

## Bailey & López de Prado (2014)

[David H. Bailey and Marcos López de Prado, *The Deflated Sharpe Ratio*, Journal of Portfolio Management 40(5)](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf).

- Eq. 1 scales the expected maximum of standardized normal trials by the dispersion of trial Sharpe estimates. Appendix 2 contains the authors' expected-maximum code.
- Eq. 2 combines the selected per-period Sharpe, raw sample length, skewness, Pearson kurtosis, and expected maximum to compute DSR.
- Appendix 3, Eq. 9 estimates independent trials as `N=ρ̄+(1−ρ̄)M`, where M is the tested count and ρ̄ is average correlation.
- The numerical example on pp. 9–10 uses annual Sharpe 2.5, 250 periods/year, T=1250, trial annual-Sharpe variance 0.5, skew −3, and kurtosis 10. N=100 gives per-period null maximum 0.1132 and DSR 0.9004; N=46 gives DSR 0.9505. With normal moments and N=88, DSR is 0.9505.
- DSR is a model-based normal-CDF significance measure. This equation does not supply a HAC correction for serial dependence or discover unrecorded prior searches.

Pinned PDFs and SHA-256 identities are in `references/papers/attribution.md`.
Fixtures RP-005, RP-007, and RP-008 retain their own attribution and tolerances.
Platform choices about bandwidth, segments, units, trial universe, and historical reports are in ADR 0073, decision 5.
