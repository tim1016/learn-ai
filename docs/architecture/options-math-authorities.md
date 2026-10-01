# Options-math authorities

**Status:** Active

Which options-math module a caller must use. The canonical-implementation rule
is [ADR 0068](adrs/0068-python-owns-the-canonical-math.md); each module's
provenance block names its own canonical function.

---

## Dispatch rules

When a caller needs price or Greeks, choose by **option style** and **TTM resolution**:

```
European option, TTM ≥ 1 day, non-comparison context
    → bs_greeks.bs_european_price + bs_greeks.black_scholes_greeks
      (closed-form, fastest, no QuantLib initialization cost)

European option, TTM < 1 day (intraday / 0DTE)
    → bs_greeks.* only.
      QuantLib's date-based engine collapses TTM to 0 calendar days
      and returns 0 Greeks. The closed-form path is the only correct one.

European option, in `/api/quantlib/compare` (curve overlay vs QuantLib)
    → bs_greeks.bs_european_price for the "python_bs" curve;
      quantlib_pricer.price_option(engine=ANALYTIC_BS) for the "quantlib_bs" curve.
      The point of /compare is to show both side-by-side; that's the one valid
      reason for two pricing paths in the same call.

American or exotic option (none today; future)
    → quantlib_pricer.price_option with the appropriate engine.
      No closed-form path exists for these.

Implied volatility, any case
    → volatility/solver.implied_volatility
```

A `compute_greeks(...)` dispatcher that encodes this is **not yet implemented**.
It becomes worth it when there is a third caller that needs to pick by style.
For now, two callers (`options_companion_service`, `/api/quantlib/*`) each pick
the right one explicitly.

---

## What does NOT belong in any of these modules

- **No math in C# or TypeScript.** The .NET resolvers are passthroughs; the Angular code is rendering. See `CLAUDE.md` § 5.
- **No new BS price formula** in any other Python file. Use `bs_european_price`.
- **No new IV solver.** Use `implied_volatility` from `app/volatility/solver.py`. If it can't handle your case, fix it there or add a documented sibling with a clear name (`implied_volatility_american`, etc.) — never a duplicate.
- **No risk-free rate constants** scattered through service modules. Use `app/services/fred_service.get_risk_free_rate(dte_days, observation_date)` for the live rate. Where a surface needs a default, it reads the one constant, `app/services/risk_free_rate.py::DEFAULT_RISK_FREE_RATE`, which is also FRED's fallback. .NET and Angular never restate it; they omit the rate and let Python fill it (#2764).
