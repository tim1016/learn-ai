# AM-006 — American option price and delta around a nearby cash ex-date

## Source

QuantLib 1.42.1 `FdBlackScholesVanillaEngine` with `CashDividendModel.Escrowed`, the Douglas scheme, 2,000 time steps and 2,000 price steps. American-exercise vanilla calls and puts use a flat 0.045 rate, flat 0.6 volatility, no continuous yield and Actual/365 (Fixed). Generated 2026-10-09.

The dividend model is the same as AM-002 and the canonical tree: the stock price less the present value of future known cash dividends follows the lognormal process. QuantLib is an independent finite-difference oracle; the generator does not import the canonical pricer.

## Cases and measured discrepancy

Each case has spot 100, strike 80, 59 days to expiry and one $3.00 cash dividend. The ex-date is day 1, day 2 or the expiry day; both call and put are included. The day-1 call is the failure case from issue #2963. With the canonical tree:

| Ex-date | Right | Explicit 401-step gap | Adaptive default gap | Explicit 2,003-step gap |
|---|---|---:|---:|---:|
| Day 1 | Call | -0.0190124 | +0.0022790 | -0.0016592 |
| Day 1 | Put | +0.0003318 | +0.0003318 | +0.0000843 |
| Day 2 | Call | -0.0017031 | -0.0019523 | -0.0011729 |
| Day 2 | Put | +0.0003317 | +0.0003317 | +0.0000843 |
| Expiry day | Call | -0.0074978 | -0.0028711 | -0.0014567 |
| Expiry day | Put | -0.0000012 | -0.0000012 | -0.0000010 |

The gaps are canonical minus QuantLib, in dollars per share. They are measurements on these six cases, not a bound for other contracts. The day-1 call is outside AM-002's cases and its explicit 401-step price tolerance of 0.005. The adaptive default uses 1,009 steps for these calls and 401 for the puts. Increasing to 2,003 steps helps, but does not reduce the day-1 call to the 1e-4 figure reported in the issue.

The default test holds every price to `atol=5e-3` and delta to `atol=1e-3`; the largest measured differences are 2.87e-3 and 2.83e-4. The explicit 2,003-step test holds price to `atol=3e-3` and delta to `atol=1e-4`; the largest measured differences are 1.66e-3 and 5.74e-5. Both use `rtol=0`.

Solving these six reference prices back to their input volatility with the adaptive default gives a largest volatility gap of 4.30e-4 and a largest delta gap of 4.79e-4. The test holds those to `atol=5e-4` and `atol=1e-3`, respectively, with `rtol=0`. For the day-1 call an explicit 401-step solve gives volatility 0.6035156 against the input 0.6; the adaptive default gives 0.5995705.

## Regeneration

Run `cd PythonDataService && .venv/bin/python -m scripts.fixture_generators.american_options` with QuantLib 1.42.1. This generates AM-001 through AM-006 from QuantLib. The pinned reference version is checked before any file is written.
