# Bouchaud-Farmer-Lillo 2008 Market Microstructure Reference

Reference: Jean-Philippe Bouchaud, J. Doyne Farmer, Fabrizio Lillo, "How markets slowly digest changes in supply and demand", arXiv:0809.0822v1.

Vendored source: `references/arxiv-0809.0822v1/source/arXiv-0809.0822v1.tar.gz`.

Design document: `docs/design/bouchaud-farmer-lillo-2008-microstructure-implementation-design.md`.

## Port Scope

This paper is a review and synthesis of market microstructure studies. It contains formulas and empirical validation results for:

- Long-memory in order flow.
- Strategic hidden-order splitting.
- Concave individual trade impact.
- Aggregate impact.
- Transient impact / propagator models.
- History-dependent permanent impact and asymmetric liquidity.
- Bid-ask spread versus impact phase diagrams.
- Spread recovery after liquidity shocks.
- Liquidity versus volatility.
- Limit-order placement and order-book shape.
- Optimal execution profiles.

The first repo implementation should prioritize optimal execution and transaction-cost modeling. Because learn-ai does not currently have tick-level trade, quote, or order-book data, the first implementation must be bar-compatible: static optimal schedules, integer child-order allocation, modeled spread/impact assumptions, and comparison against TWAP/VWAP-style bar baselines. Full empirical replication of the paper's LSE/PSE/NYSE studies requires tick, quote, order-book, and sometimes broker/member-code data that is not included in the arXiv source.

## Paper-Reported Benchmark Fixture

The paper-reported empirical validation results are preserved in:

`PythonDataService/tests/fixtures/golden/microstructure/BFL-2008-MICRO-001/v1/`

This fixture is intentionally a literature benchmark inventory. It is not a raw-data equivalence fixture. It should be used to keep reported benchmark values visible to future implementation agents and to test benchmark parser/output shape. It must not be used as the sole proof that our implementation reproduces the authors' empirical datasets.

## Implementation Guidance

When implementing any formula from this paper:

1. Read the vendored TeX around the relevant section.
2. Add a provenance block to the Python module.
3. Add or update the corresponding row in `docs/math-sources-of-truth.md`.
4. Add a fixture-backed or hand-computed test with explicit tolerances.
5. Document all deviations and data limitations.

## High-Value First Ports

1. `PythonDataService/app/engine/execution/optimal_schedule.py`
   - Section 10.
   - Formula: U-shaped optimal execution schedule under transient impact.
   - Useful for adaptive order planning such as buying 100 shares over two hours.

2. `PythonDataService/app/engine/execution/execution_cost.py`
   - Sections 5.1, 6.1, 7.
   - Formula family: spread cost plus concave impact cost.

3. `PythonDataService/app/research/microstructure/order_flow.py`
   - Section 4.
   - Formula family: sign autocorrelation, long-memory exponent, Hurst relation, hidden-order tail implication.

4. `PythonDataService/app/research/microstructure/spread_impact.py`
   - Section 7.3.
   - Formula family: spread-impact phase diagram.

## Known Limitations

- learn-ai does not currently have tick-level trade, quote, or order-book data.
- OHLCV bars cannot validate order-flow memory, trade impact, spread dynamics, or order-book gaps.
- The source archive does not include the original LSE/PSE/NYSE/Spanish datasets.
- The empirical values extracted into `BFL-2008-MICRO-001` are reported benchmark values, not regenerated outputs.
- Live/paper order routing is out of scope for the initial port.
