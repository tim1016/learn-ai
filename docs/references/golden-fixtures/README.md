# Golden Fixture Reference Docs

One markdown file per fixture, organized by category. Each file documents
the mathematical formula, oracle choice, tolerance rationale, and
regeneration command for a specific fixture.

## Categories

- `options-pricing/` — Black-Scholes price, Greeks, implied volatility, SVI
- `engine-statistics/` — Sharpe, Sortino, CAGR, max drawdown, Calmar
- `indicators/` — EMA, SMA, RSI (from LEAN vendored output)
- `volatility/` — IV solver round-trip, IV-RV basis, realized vol

## File naming

`<FIXTURE-ID>.md` — e.g. `BS-001.md`, `ENG-001.md`

Files are created alongside the fixture in PR #2 (first fixtures).
This README is created in PR #1 (foundation) to establish the directory.
