# Options — Implementation Truth Document

> **Status:** Reference note. It keeps the Polygon vendor facts the options
> surfaces work within (§3) and the documented non-equivalence of the
> probability-of-profit model (§4.5). The formulas are stated in each
> module's provenance block, and the module a caller must use is in
> `docs/architecture/options-math-authorities.md`. Section numbers are
> unchanged.

---

## 3. Hard constraints

The vendor facts every options surface works within.

| Constraint | What it rules out |
|---|---|
| **Polygon Starter plan** (aggregate history is 5 years and the boundary day is excluded — `polygon_history_floor()` in `PythonDataService/app/data_lake/polygon_fetcher.py`; 15-min delayed; no historical bid/ask) | Real backtested historical bid/ask, so historical quotes use spread synthesis (`app/volatility/price_normalization.py`). The snapshot endpoint is *live-only*: it serves only live contracts, so historical chains are rebuilt from per-contract aggregates. |

---

## 4. Mathematical foundations

The formulas behind §4.1–§4.4 and §4.6 are stated in the provenance blocks of
`app/services/bs_greeks.py`, `app/volatility/solver.py`,
`app/volatility/analytics.py` and `app/services/quantlib_pricer.py`. Only the
POP model's documented non-equivalence stays here.

### 4.5 Probability of profit (POP) for a strategy

**Equation.** Under the Black-Scholes lognormal model, the
probability that the strategy P&L at expiry is non-negative is:

$$\text{POP} = \int_{S_{\text{profitable}}} f_{\text{lognormal}}(S_T \mid S_0, r-q, \sigma, T) \, dS_T$$

where `S_{profitable}` is the union of intervals where
`compute_payoff_at_expiry(legs, S_T) ≥ 0`, and the lognormal density
uses scipy's `lognorm.pdf` with:

- `scale = S_0 · e^{(r-q)T}`
- `s = σ √T`

**Canonical source.** Hull §15.6 (lognormal property of stock prices);
[`PythonDataService/app/services/strategy_engine.py`](../../PythonDataService/app/services/strategy_engine.py)
(see `compute_pop`).

**Known limitation.** Today the POP integral assumes BS lognormal terminal
distribution. A SABR-corrected POP would be a more honest estimate near the
tails; deferred until a research-validation pipeline exists for it.

---

## 11. References

**External references:**

- Hull, J. C. (2017). *Options, Futures, and Other Derivatives*, 9th
  ed. Pearson. (§15.8 BS price, §15.9 worked example, §17.6–17.10
  Greeks, §10.4 parity, §17.13 implied dividend.)
- CBOE VIX White Paper (2019). (For the IV-pipeline cross-validation
  in `iv-ownership-research.md`.)
- Polygon.io API documentation
  ([`docs.polygon.io`](https://polygon.io/docs/options/getting-started)).
