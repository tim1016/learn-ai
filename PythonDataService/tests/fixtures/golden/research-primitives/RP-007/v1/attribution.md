# RP-007 — Lo non-IID Sharpe standard error

Source: Andrew W. Lo (2002), The Statistics of Sharpe Ratios, eq. 14–15 and Appendix. https://traders.studentorg.berkeley.edu/papers/The-Statistics-of-Sharpe-Ratios.pdf

Oracle: `scripts/fixture_generators/sharpe_inference.py`, using 60-digit Decimal scalar influence scores q=z−SR(z²−1)/2, with Bartlett-weighted within-segment lag products. Production independently uses a 2×2 covariance matrix and gradient. Synthetic cases cover lag zero, serial covariance, and two separate segments. These are equation-derived fixtures, not published observations or measured coverage guarantees.

Conventions: denominators T; population-variance Lo point and SE multiplied by sqrt((T−1)/T) to match sample-SD Sharpe; display scaling sqrt(252). Segment boundaries assume zero cross-segment covariance. Bandwidth and this finite-sample rescaling are explicit platform choices in ADR 0073.

Tolerance: atol=1e-12, rtol=0 for point, SE and interval bounds. Decimal-to-float conversion and the standard-library normal quantile are the only oracle precision limitations. Regenerate with the project Python: `PythonDataService/.venv/bin/python scripts/fixture_generators/sharpe_inference.py`.
