# AM-003 — American implied volatility and the delta at it

## Source

QuantLib 1.42.1, `BinomialLRVanillaEngine` at 401 steps: the prices and deltas of fixture AM-001. Each case gives a solver the price QuantLib computed at a known volatility. The solver must return that volatility, and the delta QuantLib reported at it. Generated 2026-10-06.

## What is in it

140 cases: every 401-step case of AM-001 whose price holds at least $0.0001 more than exercising at once pays. The rest are left out because a price at its exercise value implies no volatility; the canonical solver's refusal of those is tested beside it, not here.

## Assumptions

As AM-001. The oracle is the volatility the price was generated at, so the fixture proves the solver inverts the pricer, and inherits AM-001's proof that the pricer is QuantLib's.

No case here has a dividend. The solver with dividends is checked against AM-002's prices, at that fixture's looser tolerance.

## Independent numerical oracle

`reference_kind=external_reference`. The generator calls QuantLib only and never imports the canonical implementation.

## Tolerance

Volatility `atol=1e-6`, delta `atol=1e-9`, `rtol=0`. The canonical tree and QuantLib's agree to about 4e-10 in price; turned back into a volatility that gap is divided by the option's vega, which far from the money is small. Largest gap measured over the 140 cases: 7.2e-8 in volatility, 3.4e-11 in delta.

## Regeneration

`cd PythonDataService && .venv/bin/python -m scripts.fixture_generators.american_options` It needs QuantLib 1.42.1 (`.venv/bin/pip install QuantLib==1.42.1`): the service's own requirement allows newer releases, and the generator refuses to write these fixtures from any other.
