# [Codex] Validated strategy to live bot semantic equivalence

Baseline: `10b5f31b529c8507bd19bb24015f3d85fa9aba43`. Independent first pass; no prior maps/audits, broker state, environment files, services, or vendor endpoints read. Synthetic-only tests ran in an isolated clone with network, protected-path, subprocess, and outside-directory write guards. No production edits.

## Finding S1: decision price overwrites the current valuation price

- **Claim:** A backtest consolidator callback replaces the current minute's portfolio mark with the preceding signal bucket's close, and the engine publishes that stale value under the current minute's timestamp.
- **Goal:** Correctness; trading intelligence; backtest/live seam.
- **Severity:** High. The demonstrated defect affects intermediate marked equity and statistics derived from it; this finding does not claim that final realized P&L is wrong.
- **Evidence — reproduced:** [engine.py:304](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/engine/engine.py#L304) first writes the current source close. [base.py:168–172](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/engine/strategy/base.py#L168) then replaces that same field with the just-emitted signal bar's older close. [engine.py:490–495](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/engine/engine.py#L490) scores insights and snapshots equity without restoring the current mark. [engine_backtest_service.py:963–983](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/engine_backtest_service.py#L963) feeds those snapshots to ordinary backtest statistics. The failing invariant is [test_codex_2417_marks.py](https://github.com/tim1016/learn-ai/blob/research/codex-2417-seam/PythonDataService/tests/review/test_codex_2417_marks.py): after buying 100 shares at $100, the minute closing at $101 reports equity $10,000 instead of $10,100. Prices are exact Decimal values, with zero fees and slippage to isolate marking.
- **Why it matters:** At signal emission boundaries, historical equity and risk observations can lag by one input bar even though the engine has already observed the newer close. Drawdown, intermediate P&L, and insight scoring can disagree with the prices at their own timestamps. The live adapter explicitly scans at a source bar's own close ([bot_trade_strategy.py:509–527](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/bot_trade_strategy.py#L509)); sharing the decision algorithm alone does not prove equivalent accounting clocks.
- **Recommendation:** Give decision-time pricing and current portfolio valuation separate authorities, or scope/restore the decision reference before current-time valuation. Preserve intentional signal-close fill semantics while requiring every equity snapshot and scored price to identify the observation clock it uses. Add a behavioral reconciliation across signal time, fill time, and valuation time.
- **Confidence:** High for the shown engine behavior and reachable statistics consumer; medium for the magnitude of strategy ranking changes, which depends on price paths and metrics. Evidence of an intentional delayed-valuation product contract or a later valuation correction before every consumer would change the assessment. Neither exists in the traced path.

## What is supported by evidence

- Registered live strategies and Engine Lab share the registry's Signal Program factory. The live adapter rejects retired/unregistered programs rather than fabricating a runtime (`bot_trade_strategy.py:460–505`).
- SignalSession separates staging from COMMIT/DISCARD, rejects wrong-width/non-monotonic bars and unsettled stages, and irrevocably discards observe-only decisions (`signal_program.py:178–238`).
- New seals record effective parameter values and factual origins, symbol, cadence, account/action plan/quantity and validation identity. Admission checks current files against a qualification receipt and checks the seal contract (`signal_program_admission.py:122–242,372–478`). The provider field explicitly means qualification lineage; IBKR live bars versus Polygon qualification is documented, not a hidden provider switch (`signal_program_seal.py:75–89`).
- Warmup replays recorded dispositions and distinguishes retained live observations from history; resume tail-join tests cover refusals and deterministic stitching. These are meaningful controls, not proof that historical vendor data equals live IBKR observations.

## Validation and coverage

- **65 passed**: `tests/services/test_signal_program_admission.py`, `test_run_replay_engine_parity.py`, `test_retained_tail_join.py` under the isolated guard.
- **1 failed as intended**: `tests/review/test_codex_2417_marks.py`, exact current-price equity invariant shown above. Production source remains unchanged.
- Traced canonical program construction, staging, seal/admission, warmup/replay, source-feed lineage, engine valuation and statistics. Earlier investigations separately own data-lake input quality and account custody; those findings are not duplicated here.
- No claim about current live or paper account behavior is based on runtime state.

## Newly sharp questions

1. Does a receipt over current on-disk source prove the code already imported by a long-lived clerk process, especially with the documented source bind mount? This requires a separate code-identity investigation.
2. Do temporal discontinuities, early closes, and retained/live handoffs preserve decisions and clock ownership beyond the passing first-pass tests? A focused boundary matrix can assess this independently of the valuation defect.
