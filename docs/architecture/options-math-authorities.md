# Options-math authorities

**Status:** Active
**Last reviewed:** 2026-04-29 (Phase 1 of options-routes cleanup —
[docs/architecture/options-routes-research.md](options-routes-research.md))
**Owner of this doc:** the person editing options-math code

This document is the answer to "where does the canonical implementation of
*X* live?" for everything in the options stack. It exists because we previously
had two `implied_volatility` functions that could disagree silently (see PR
[cleanup/options-math-sovereignty](https://github.com/) for the consolidation).

`CLAUDE.md` § 5 — *one authority for any given numerical answer in the system* —
is the rule this document operationalizes.

---

## Single source of truth, by calculation

| Calculation | Canonical module | Function | Notes |
|---|---|---|---|
| **Black-Scholes European price** | `app/services/bs_greeks.py` | `bs_european_price` | Closed-form, continuous time. Accepts continuous dividend `q` (default 0). |
| **Black-Scholes raw vega** | `app/services/bs_greeks.py` | `bs_european_vega` | Per 1.0 vol unit (Newton-Raphson convention). Use `black_scholes_greeks(...).vega` for the per-1% UI value. |
| **Black-Scholes Greeks (delta/gamma/theta/vega/rho)** | `app/services/bs_greeks.py` | `black_scholes_greeks` | Closed-form, continuous time. Sub-day-resolution safe — chosen for the 0DTE companion path. |
| **Implied volatility (single contract)** | `app/volatility/solver.py` | `implied_volatility` | QuantLib `VanillaOption.impliedVolatility()` primary, scipy `brentq` fallback, returns `ImpliedVolResult` with diagnostics. Has `min_ttm` for intraday callers. |
| **Implied volatility (vectorized chain)** | `app/volatility/solver.py` | `solve_iv_chain` | Loop wrapper around `implied_volatility`. |
| **Volatility surface fitting (SVI, SABR, variance interpolation)** | `app/volatility/fitting.py` | (multiple) | Per-expiry smile fits used by `surface.py`. |
| **Skew metrics (RR-25, BF-25, slope)** | `app/volatility/analytics.py` | `compute_skew_metrics` | Per-expiry. |
| **Forward price from put-call parity** | `app/volatility/analytics.py` | `compute_put_call_parity_forward` | Returns implied forward `F` per TTM; `q` is derived as `r - ln(F/S)/T` — there is no separate function for `q` yet. |
| **QuantLib pricing engine + numerical Greeks** | `app/services/quantlib_pricer.py` | `price_option`, `price_strategy`, `implied_volatility` | Used when QuantLib's pricing path matters (multi-engine `/compare`, American/exotic options if added). Greeks here use QL analytical when supported, numerical bumps otherwise. **Note:** the QuantLib IV path (`quantlib_pricer.implied_volatility`) is *internal* to the QuantLib branch of `volatility/solver.implied_volatility`'s fallback chain — direct callers should use `volatility/solver.implied_volatility`, not this. |
| **Engine-side option pricing (Lean-style strategies)** | `app/engine/options/pricer.py` | `price_contract`, `price_contract_from_market` | **Adapter** around `quantlib_pricer.price_option`. Repackages `GreeksResult` → engine `OptionGreeks` dataclass. Not a separate Greeks authority — do not add new Greeks formulas here. |

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
For now, three callers (`options_companion_service`, `engine/options/pricer`,
`/api/quantlib/*`) each pick the right one explicitly.

---

## What does NOT belong in any of these modules

- **No math in C# or TypeScript.** The .NET resolvers are passthroughs; the Angular code is rendering. See `CLAUDE.md` § 5.
- **No new BS price formula** in any other Python file. Use `bs_european_price`.
- **No new IV solver.** Use `implied_volatility` from `app/volatility/solver.py`. If it can't handle your case, fix it there or add a documented sibling with a clear name (`implied_volatility_american`, etc.) — never a duplicate.
- **No risk-free rate constants** scattered through service modules. Use `app/services/fred_service.get_risk_free_rate(dte_days, observation_date)`. The one remaining `DEFAULT_RISK_FREE_RATE = 0.043` in `iv_builder.py` is a function-default fallback for tests; every production call site overrides it.

---

## History (why this doc exists)

**Before 2026-04-25:**
- `app/research/options/bs_solver.py` defined `bs_price`, `bs_vega`, `implied_volatility` (Newton-Raphson + Brent fallback, pure scipy, hard-coded `RISK_FREE_RATE = 0.043`).
- `app/volatility/solver.py` defined a different `implied_volatility` (QuantLib + Brent fallback, with `ImpliedVolResult` diagnostic, intraday `min_ttm` support).
- `iv_builder.py` consumed the first; `surface.py`, `options_companion_service.py`, and `volatility/__init__.py` consumed the second.
- The two could disagree — different convergence criteria, different IV bounds, different rate handling.

**On 2026-04-25:**
- `bs_solver.py` was deleted.
- `bs_european_price` and `bs_european_vega` were added to `services/bs_greeks.py` (the only file that legitimately needs them, since it was already the home of the closed-form Greeks).
- `iv_builder.py` was migrated to `volatility/solver.implied_volatility` with the legacy IV-bounds gate preserved at the call site.
- `routers/quantlib_options.py` was migrated to `bs_greeks.bs_european_price` for the python-vs-QuantLib comparison curve.

The duplication is gone. New options-math work follows the dispatch rules above.
