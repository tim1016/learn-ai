# RP-005 — Lo (2002) IID Sharpe standard error

Generated: 2026-10-23

Reference: Andrew W. Lo, “The Statistics of Sharpe Ratios,” *Financial Analysts Journal* 58(4), 2002, Eq. 9 and Table 1, pp. 38–39. [Author paper](https://traders.studentorg.berkeley.edu/papers/The-Statistics-of-Sharpe-Ratios.pdf). Table 1 reports standard errors 0.188 at Sharpe 1.50, T=60 and 0.303 at Sharpe 3.00, T=60.

Regenerate: `python3 scripts/fixture_generators/lo_sharpe_ci.py` from the repository root. The script transcribes the two published table entries and constructs deterministic returns with sample standard deviation one and the stated sample Sharpe. No canonical implementation is used to generate the oracle outputs.

Parameters: 60 observations per case; `bars_per_year=1`; `T=60`. The two-valued returns are synthetic test inputs, not an IID random draw. They isolate the paper's IID formula in `signal/sharpe.py::compute_iid_sharpe_ci` and do not validate the platform's autocorrelation correction or annualization policy.

Tolerance: `atol=0.0005, rtol=0`. The paper rounds standard errors to three decimal places; this is the half-unit rounding interval. It is a publication precision limit, not a numerical drift allowance.
