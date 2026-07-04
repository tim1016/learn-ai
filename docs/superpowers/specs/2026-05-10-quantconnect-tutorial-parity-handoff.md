# QuantConnect ML-tutorial parity study — session handoff

> **Status (2026-05-10): superseded for Phase 1.** The Phase 1 parity target is QuantConnect's *precomputed-predictions* tutorial, not the Keras "ML key concepts" tutorial framed in this doc. See `docs/superpowers/specs/2026-05-10-quantconnect-precomputed-predictions-parity.md` for the Phase 1 spec and the Phase 1 / Phase 2 / Phase 3 split. This doc is kept on disk because its **Hard problems / risks** (lines 79–92) and **Open questions** (lines 113–123) sections still inform Phase 2 (Keras tutorial parity). When Phase 2 starts, revisit this doc.

**Status:** superseded — see banner above
**Date:** 2026-05-10
**Author:** Claude (with Tim) — handoff written at the end of the v0.5 plumbing session
**Predecessors:**
- `docs/superpowers/specs/2026-05-09-ml-prediction-as-data-v05-design.md` — the v0.5 plumbing spec (merged in #207, #208, #209)
- v0.5 plumbing plan — pruned 2026-07-04; see git history

## Why this study (Tim's belief)

> "First we need to exactly reproduce what QuantConnect does as they show in their tutorials. So we can have confidence that what we have built is actually matching another implementation."

This is the same numerical-rigor pattern the repo already uses for indicators and strategies (LEAN parity for SMA / EMA / RSI mean-reversion). Before innovating on top of v0.5, prove that v0.5 + an ML stack can reproduce a published reference end-to-end. Anything we discover ("bit-exact is infeasible because TF nondeterminism," "QC's data feed differs from our LEAN cache by X," etc.) becomes documented constraints for v1.

## What's already done (state at start of next session)

`master` is at `abeafd7` (v0.5 plumbing + P1 cleanup). The next session inherits:

- **`app/research/ml/`** — artifact format, loader, bar-clock coverage, deterministic-rule generator, CLI.
- **`StrategySpec.predictions: list[PredictionRef]`** + **`PredictionComparison`** condition kind in `app/engine/strategy/spec/schema.py`.
- **`EvalContext.predictions: dict[str, Decimal]`** populated per bar by `SpecAlgorithm` in `app/engine/strategy/spec/evaluator.py`.
- **`RunLedger`** schema 1.1 with optional `prediction_set_hash` (`app/research/runs/ledger.py`); legacy 1.0 ledgers still load.
- **`run_strategy_spec`** loads any declared prediction set, runs bar-clock coverage check, threads `prediction_set_hash` into the ledger (`app/research/runs/runner.py`).
- **`to_ms_utc`** centralized at `app/utils/timestamps.py`.
- **Pinned hash** for the deterministic-rule generator (`rsi_14_centered`, 30-bar synthetic input):
  `433a59131b81b0b7bce966b9ec88a2178caea784e67e721b5db18d6482f43d2c`
  — committed at `tests/research/ml/fixtures/e2e_known_hashes.json`.

**What's NOT in place:**
- No sklearn / Keras / TF dependencies. `requirements-heavy.txt` does not include any of them.
- No `ModelSpec` schema. The v0.5 generator is a hardcoded rule (`rsi_14_centered`); there is no declarative way to describe a trained model yet.
- No leakage-test invariants beyond the bar-clock coverage check.
- No behavioral-equivalence contract for non-deterministic model output.

## The study scope

Reproduce the QuantConnect "ML key concepts" tutorial that Tim pasted in chat. Six steps:

1. **Hypothesis:** next-day SPY close % change predicted by previous 5 days of OHLCV % changes.
2. **Model:** Keras `Sequential` MLP — `Dense(10, input_shape=(5,5), activation='relu')` → `Dense(10, activation='relu')` → `Flatten()` → `Dense(1)`. RMSprop(0.001), MSE loss.
3. **Data prep:** 360 days of daily SPY history via `qb.History(... 360, Resolution.Daily)`. Compute `pct_change()`. Window into 5-day blocks; label = next day's close pct_change.
4. **Train:** 300 days train / 60 days test. 5 epochs.
5. **Validate:** plot predicted vs. actual % change on the validation set.
6. **Deploy:** save serialized model to `qb.ObjectStore`. Trading algorithm reads it and acts on prediction sign.

Sources:
- Tim's chat paste (canonical text of the 6 steps + code).
- QC docs: `https://www.quantconnect.com/docs/v2/writing-algorithms/machine-learning/key-concepts`, `.../training-models`, `.../precomputed-ml-predictions` (Tim referenced these in the original framing of the platform conversation).

## Success criteria (the hard question)

The framing word "exactly" needs disambiguation in the next session. Two regimes:

**Regime A — Bit-exact parity.** Identical predictions to the last bit. Requires:
- Identical input data (same SPY 360-day window, same dividend/split treatment).
- Identical sklearn / Keras / TensorFlow / NumPy / BLAS versions.
- Forced CPU execution (`CUDA_VISIBLE_DEVICES=""`).
- Disabled XLA, disabled mixed precision.
- All seeds set in the right order: `PYTHONHASHSEED`, `random.seed`, `numpy.random.seed`, `tf.random.set_seed`, `tf.experimental.numpy.random.seed`.
- Op-order determinism flags (`TF_DETERMINISTIC_OPS=1`).

Even with all of the above, exact reproduction across pip-installs of TF on different machines is fragile. **My read is this is technically infeasible without running on QC's cloud and capturing their exact outputs as fixtures.**

**Regime B — Behavioral parity within documented tolerance.** Define a divergence taxonomy and a tolerance per category:

| Comparison | Tolerance | Why |
|---|---|---|
| Input bar timestamps and OHLCV values | bit-exact | Data identity is non-negotiable. If our SPY data doesn't match QC's, fix it before running the model. |
| Feature matrix (`X_train`, `X_test`) shapes | bit-exact | Trivially reproducible. |
| Feature matrix values | `atol=1e-12, rtol=0` | `pct_change()` is deterministic; any drift here is a data bug. |
| Model architecture (layer count, shapes, activations, optimizer config) | bit-exact | Configuration, not output. |
| Trained weights | none — non-comparable across TF builds | Don't bother. |
| Validation MSE | within ±10% of QC's published value | Bounds the model's correctness without demanding bit-exact training. |
| Per-row predicted-vs-actual chart shape | qualitative match (sign agreement on >70% of points, ranking correlation > 0.5) | Trading edge is what matters, not pointwise prediction. |

**Recommendation:** target Regime B with the table above. Document any per-comparison divergence that exceeds tolerance. Present Tim with the parity report; he decides whether the divergence is acceptable or requires further investigation.

## Hard problems / risks

1. **TF / Keras non-determinism** — covered above. Even Regime B requires deterministic local runs (so we can re-run and assert hash stability over time on our infra) plus a frozen `requirements-heavy.txt` pinning.
2. **Data alignment.** QC's `qb.History("SPY", 360, Resolution.Daily)` returns daily bars from QC's data feed. We have:
   - LEAN minute data → consolidate to daily
   - Polygon daily aggregates
   These may differ on dividend-adjusted close, after-hours treatment, half-day handling. **First-step before any modeling**: fetch the same 360-day window from both sources, diff, document divergences.
3. **`qb.History` semantics.** QC's API may use end-anchored windows ("the 360 days ending today"). Pinning a specific calendar window (e.g. 2024-04-01 to 2025-03-26) makes the study deterministic across re-runs.
4. **`qb.ObjectStore` vs. our artifact format.** QC saves a serialized Keras model and reads it in their algorithm. v0.5's contract is **predictions as data, not model as data** — we precompute predictions and store those. Two paths:
   - (a) Train Keras locally → predict for every bar → store predictions in our `app/research/ml/` artifact format → consume via `PredictionComparison` in a `StrategySpec`. **Matches v0.5's design and the original spec's "plumbing-before-model" decision.**
   - (b) Mirror QC's pattern — save the trained model, load it inside the strategy, predict at runtime. **Goes against v0.5's design but is faithful to QC's tutorial.**

   **Recommendation:** path (a). The "exact reproduction" is at the level of **what predictions the model produces given the same inputs**, not the runtime mechanism. Document the deviation from QC's runtime path explicitly: we precompute, they train-and-load. The numerical results match either way.
5. **Heavy dep footprint.** Keras + TensorFlow add ~500MB to the Docker image and meaningful CI install time. Sklearn-only would be much lighter, but the QC tutorial uses Keras, so we're stuck with it for parity. **Mitigation:** keep TF in a separate optional requirements file (`requirements-tf.txt`) gated behind an env var or pytest marker; CI installs it for ML tests but the regular Python service container does not.

## First concrete moves for the next session

The session should NOT skip straight to coding. Recommended ordering:

1. **Invoke `superpowers:brainstorming`** with this handoff as input. The session's first job is to confirm the success criterion (Regime A vs. B), agree on the data source, and decide path (a) vs. (b) for the runtime model question.
2. **Capture QC ground truth.** If Tim has a QC account, run their notebook in their cloud and save:
   - The 360-day SPY data (CSV).
   - The first 5 rows of `df.pct_change()`.
   - `X_train.shape`, `X_test.shape`, `y_train.shape`, `y_test.shape`.
   - The 5-epoch training loss values.
   - Validation MSE.
   - The full `y_test` and `y_hat` arrays from `model.predict(X_test)`.
   - A screenshot of the validation plot.
   These become the **golden fixtures** for the parity test.
3. **If no QC account access:** acknowledge Regime A is impossible (no ground truth to compare against), and the parity study reduces to "we built a faithful re-implementation of their tutorial's STRUCTURE and observed our outputs are well-behaved." Document that explicitly and move to Regime B with our own outputs as the new baseline.
4. **Pin TF / Keras determinism configuration locally** before running the tutorial. Run the same script twice on our infra; assert byte-identical predictions. If we can't even achieve local determinism, the parity report is meaningless.
5. **Brainstorm the ModelSpec schema** for v1. The QC tutorial's parameters (input shape, layer sizes, activations, optimizer, learning rate, epochs, train/test split) become the first concrete fields. Match the declarative pattern of `StrategySpec` — `extra='forbid'`, content-addressable hash.
6. **Write the spec and plan.** Same brainstorm → spec → plan → subagent-driven flow as v0.5.

## Open questions for Tim before the session starts

These should be answered (or at least surfaced) before any code is written:

1. **Do you have a QuantConnect account / can we run their notebook?** This determines Regime A feasibility.
2. **Bit-exact parity or behavioral parity within tolerance?** I recommend behavioral; you may have a stricter view.
3. **OK to add TensorFlow + Keras as heavy deps?** They add ~500MB. Sklearn alternatives exist but break parity.
4. **Data source — LEAN cache or Polygon or QC-equivalent?** Each has trade-offs; depends on what QC's feed actually returns.
5. **Path (a) precompute predictions or path (b) load model in strategy?** I recommend (a) — fits v0.5's design. Open to your view.
6. **Calendar window** — pin a specific 360-day range (e.g. `2024-04-01` to `2025-03-26`) so the study is deterministic across re-runs. Pick one.
7. **Success delivery** — a parity report doc + a parity test in `tests/research/ml/`. Acceptable, or do you want something else?

## Useful pointers

- v0.5 spec (decisions table): `docs/superpowers/specs/2026-05-09-ml-prediction-as-data-v05-design.md`
- v0.5 plan (TDD task structure): pruned 2026-07-04; see git history
- v0.5 generator: `PythonDataService/app/research/ml/generate_prediction_set.py`
- v0.5 generator example: `PythonDataService/app/research/ml/generators/deterministic_rule.py`
- v0.5 artifact format: `PythonDataService/app/research/ml/artifact.py`
- Existing parity-test pattern: `PythonDataService/app/engine/strategy/spec/tests/test_spec_sma_parity.py`, `_parity_helpers.py`
- Existing reconciliation taxonomy: `.claude/skills/reconcile-backtest/SKILL.md`
- LEAN data source code: `PythonDataService/app/engine/data/` (look for `LeanMinuteDataReader`)
- Repo philosophy: `CLAUDE.md` § "Guiding philosophy" + `.claude/rules/numerical-rigor.md`

## Suggested session opener

Drop into a fresh session with this prompt:

> Read `docs/superpowers/specs/2026-05-10-quantconnect-tutorial-parity-handoff.md` end-to-end. Answer the seven Open Questions inline (or ask me to). Then propose a brainstorming session via `superpowers:brainstorming` to converge on the spec. Don't touch code until we've agreed on success criteria, data source, and the runtime-model path.
