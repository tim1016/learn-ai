# RP-008 — Deflated Sharpe Ratio

Source: David H. Bailey and Marcos López de Prado (2014), The Deflated Sharpe Ratio, eq. 1–2; Appendix 3 eq. 9; numerical example pp. 9–10. https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf

Published oracle: annual Sharpe 2.5, 250 periods/year, T=1250, cross-trial annual Sharpe variance 0.5. With skew=−3 and Pearson kurtosis=10, N=100 yields SR0=0.1132 per period and DSR=0.9004; N=46 yields DSR=0.9505. With normal moments (0,3), N=88 yields DSR=0.9505. Transcribed directly from the paper, not calculated by production code. Tolerance atol=0.00005, rtol=0 is half the last published decimal.

Additional synthetic grid: independent standard-library generator computes sample Sharpe spread, bias-corrected skew/Pearson kurtosis, mean pairwise return correlation, and Appendix 3 effective N. This tests integration of measured inputs; its moments are finite-sample platform conventions. Tolerance atol=1e-12, rtol=0 against scalar Python/statistics.NormalDist oracle. All grid Sharpes are per-period; production annualizes only display values.

Regenerate: `PythonDataService/.venv/bin/python scripts/fixture_generators/sharpe_inference.py`. No production code, NumPy, or SciPy used by generator. These fixtures do not prove DSR calibration for serially correlated returns or account for searches outside this grid.
