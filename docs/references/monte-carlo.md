# Monte Carlo trade-path simulation

**Concept**: Take the trade list from a persisted `RunLedger`, simulate N alternate paths over the per-trade `pnl_pct` array, and aggregate into equity bands + drawdown/streak/terminal-PnL quantiles + drawdown-breach probabilities. Answers "what range of paths are normal for this strategy's distribution?"

**Reference**: Standard non-parametric bootstrap, Efron (1979) "Bootstrap Methods: Another Look at the Jackknife"; reshuffle/permutation testing in trading is well-established and documented in López de Prado, *Advances in Financial Machine Learning* (2018) §7. **Verify both citations on next touch** — the specific method names (reshuffle / resample / forward projection) are repository-internal labels, not direct quotes from either reference.

**Canonical implementation**: `PythonDataService/app/research/monte_carlo/` and `app/routers/monte_carlo.py`. The aggregation, storage, failure semantics and caps are documented there.

## Two simulation methods

| Method | What it does | When to use | Output length |
|---|---|---|---|
| **Reshuffle** | Permute the input returns array. Same multiset, different order. | Test path dependence: if the strategy's edge is real, the order shouldn't matter much. | Always equals input length. |
| **Resample** | Sample the input returns with replacement. May produce duplicates of any input return. | Test sample sensitivity (standard bootstrap when `size = len(returns)`); forward projection (`size > len(returns)`) under an IID-returns assumption. | Caller-controlled (`projection_trade_count`). |

Both are deterministic given a `random_seed` — same seed → identical simulations across machines via `numpy.random.default_rng(seed)`.

**Reshuffle terminal-equity invariance.** Because reshuffle preserves the multiset and equity compounding is multiplicative, every reshuffle simulation lands at the *same* terminal equity (commutativity of multiplication). This is a real mathematical fact, not a quirk: `prod(1 + r_pi(i))` for any permutation π is the same value. The test suite pins this with `test_reshuffle_terminal_equity_is_constant_across_sims`, which asserts `equity_bands[-1].p5 == p50 == p95` for reshuffle output.
