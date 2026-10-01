# Cleanup plan — math reference notes and reconciliation reports (#2714)

Part of the lean-and-mean map (#2700). Plan only — nothing is deleted from this branch.

- **Read at:** `87b8e261021ec673c2e7c80448c0973bccd45378` (`origin/master`, the same SHA the map was charted at).
- **Area:** every file under `docs/references/` (123 files plus two `.gitkeep`), and `docs/math-sources-of-truth.md` and `docs/math-rigor.md`.
- **Method:** a throwaway script listed the inbound links to every note (`git grep` on its basename), grouped them by kind of source (code, test, fixture, ADR, link-checked canonical doc, sediment), and flagged code paths named in each note that no longer exist (each checked against `git log --diff-filter=D`). For math notes, I compared the note with its construct's fixture attribution, the golden `manifest.json` entry and the parity test. For the `golden-fixtures/` notes, a second script diffed each note against its fixture's `attribution.md` (tolerance tokens, numbers, words found only in the note).

## How each note was judged

- **Math notes** (the ticket's bar): keep a note only if it holds something the fixture attribution and the parity test don't: an accepted divergence and its reason, a documented non-equivalence, or a tolerance justification found nowhere else. Otherwise cut. If the port's code is dead, the note goes with it.
- **Non-math notes in `docs/references/`.** About 25 broker, clerk, data-lake and feed design references are filed here even though they are not math ports. I judged them by the map's doc-read rule (something live links them) and its sediment rule (retired behavior). The money-path rule applies: when unsure, keep.
- **Notes that sealed files cite.** A note cited from a *hashed* fixture attribution (`manifest.json` `file_sha256` covers `attribution.md` for ENG-006..009, FEE-001, RD-001, FQ-001, PNL-001) is kept. Cutting it would force a re-hash of a sacred fixture.

Verdict key: **keep** · **slim** (drop restated attribution or retired sections, keep the unique fact) · **merge** (fold several notes for one construct into one) · **cut**.

## Rows — math port notes

| Path | Verdict | Evidence |
|---|---|---|
| `docs/references/adx.md` | cut | Its one unique fact, the pandas-ta SMA-seeding divergence, is already in `app/engine/tests/test_adx.py:9`. Tolerance `1e-9` is in `golden/adx_14/attribution.md`. "Open items" is a wishlist. |
| `docs/references/macd.md` | cut | Default tolerances only (`macd.md` "Tolerance"). `golden/macd_12_26_9/attribution.md` and `test_macd.py` cover everything. |
| `docs/references/supertrend.md` | cut | The one-bar direction deferral vs pandas-ta is stated in `golden/supertrend_10_3/attribution.md:17`. Tolerances are defaults. |
| `docs/references/rma.md` | merge → `pandas-ta-dispatch.md` | Pass-through with no fixture or test. The only fact is "covered indirectly by ADX/Supertrend". Becomes one entry in the dispatch note. |
| `docs/references/rsi.md` | merge → `pandas-ta-dispatch.md` | Move one fact: the Data Lab path emits raw pandas-ta from bar `length` with no 3×length mask, unlike `services/ta_service.py`. Re-check it against `data-lab-indicator-warmup.md` (#2611) before moving. The engine-test path it names (`app/engine/tests/test_rsi.py`) does not exist. |
| `docs/references/sma.md` | merge → `pandas-ta-dispatch.md` | Nothing unique. "Open items" is a wishlist for an engine-vs-pandas-ta parity test. Its named test path `app/engine/tests/test_sma.py` does not exist. |
| `docs/references/vwap.md` | merge → `pandas-ta-dispatch.md` | Move one fact: Polygon's per-bar `vwap` field is not the cumulative session VWAP the Data Lab selector produces. |
| `docs/references/pandas-ta-dispatch.md` | keep (merge target), slim | The only record that 16 Data Lab indicators are pass-throughs, equal by reference to pinned pandas-ta 0.4.71b0, with no port and no fixture (`pandas-ta-dispatch.md:8-27`). Slim each "Math summary" to its defaults and quirks, since the formulas restate pandas-ta. Fix the stale pin location: it names the deprecated `requirements.txt` (`:16`). |
| `docs/references/options-bs-greeks-2026-04-24.md` | cut | Its Greek unit conventions (theta /365, vega and rho /100) are in the BS-005/006/007 attributions. Tolerances are in `manifest.json`. QuantLib parity is proven by `test_bs_cross_engine_parity.py`. |
| `docs/references/hull-greeks.md` | cut | Reference extract with no inbound links. The Hull §15.8/§19 citations are in each BS fixture's attribution (for example `BS-004/v1/attribution.md:8`). |
| `docs/references/bacon-max-drawdown.md` | cut | No inbound links. The code it lands in, `Backend/Services/Implementation/BacktestService.cs`, was deleted in `1eb0984a`. Bacon is cited in `ENG-002/v1/attribution.md:9`. |
| `docs/references/sharpe-ci-and-deflated-sharpe.md` | keep | Documented non-equivalence (no LEAN or mlfinlab implementation uses the same `N_eff` substitution) plus the only worked golden values. There is no fixture (`sharpe-ci-and-deflated-sharpe.md:59`). Linked from `docs/feature-runner-authority.md:762` and `docs/indicator-reliability-authority.md:328`. |
| `docs/references/lean-engine.md` | cut | "What was NOT ported" is stale: it says "learn-ai is research-only; no live trading". The pinning policy restates numerical-rigor "Sovereignty". The vendored source is attributed in `references/lean/7986ed0…/attribution.md`. |
| `docs/references/lean-set-holdings.md` | slim | Keep "Known divergences" and the fee-aware sizing path. Drop the two dated history sections: the live-path adapter `order_sizer.py` was deleted in `457787e0` (#2602). Also drop the restated source and fixture blocks. Cited by ADR 0009 and `app/engine/execution/sizing.py`. |
| `docs/references/lean-deci-cent-encoding.md` | keep | No golden fixture. Holds the round-half-up encoding tolerance and its reason. Cited by `app/data_lake/lean_writer.py`. |
| `docs/references/lean-factor-file-dividend-pricing.md` | keep | Holds "why it was wrong, in both directions", the documented non-equivalence of the earlier pricing. Cited by `golden/lean-factor-file-aapl/attribution.md` and the vendored LEAN attribution. |
| `docs/references/lean-native-statistics-oracle-v1.md` | slim | Keep the formulas for all 66 fields and the rounding/tolerance contract. Drop "Acceptance commands" and "UI interpretation" (they restate tests and UI). Linked from `math-sources-of-truth.md` (link-checked) and `contracts/strategy-lab/analytical-metric-catalog-v1.json`. Not diffed line by line against `golden/lean-statistics-oracle-v1/`; see Not reviewed. |
| `docs/references/iv-rv-basis-alignment.md` | keep | Bias size and "What this does NOT fix" are a documented non-equivalence. Cited by `app/volatility/basis.py`, `app/engine/edge/vrp.py`, `scripts/fixture_generators/volatility.py:1152` and `RV-003/v1/attribution.md`. |
| `docs/references/data-lab-indicator-warmup.md` | keep | Holds the accepted warm-up tolerance for #2611 (section "Accepted tolerance"). Cited by `app/services/indicator_warmup_policy.py` and its test. |
| `docs/references/strategy-metric-help.md` | keep | The version-2 formula change ("include the first evaluated session") is cited by `tests/fixtures/test_strategy_metric_help_golden.py`. |
| `docs/references/strategy-lab-analytical-manual-v1.md` | cut | No inbound links. The claim ledger is pinned by `golden/strategy-lab-analytical-manual-v1/` and `tests/contracts/test_strategy_lab_analytical_manual_fixture.py`. Re-check before cutting that the trader-language source list appears in the fixture attribution. |
| `docs/references/strategy-abc-self-equivalence.md` | keep | `manifest.json:1497` uses it as ENG-008's citation, and ENG-008's attribution is hashed. Cutting it forces a sealed-fixture edit. |
| `docs/references/strategy-spec-layer.md` | slim | Keep "Why no golden fixture?", which justifies the equivalence level. Drop "Authority cross-references" and the stale `spy_ema_crossover.py` path (deleted in `c35884b6`, #1865). Linked from `engine-authority-map.md` and `math-sources-of-truth.md` (both link-checked). |
| `docs/references/realized-equity-staircase-v1.md` | keep | `manifest.json:1453` uses it as the fixture citation. Linked from `math-sources-of-truth.md` (link-checked) and three code files. |
| `docs/references/engine-validation-analytics.md` | keep | Formulas with no golden fixture. The fixture tests that `engine-lab-runs-75-76…` names for it do not exist. Linked from `math-sources-of-truth.md`. |
| `docs/references/broker-v2-fifo-pnl.md` | keep (money path) | Cited by `app/services/account_activity.py`, `catalog_projection_service.py` and `math-sources-of-truth.md` (link-checked). Not diffed against `golden/broker-v2-fifo-pnl/`; see Not reviewed. |
| `docs/references/broker-v2-readiness-summary.md` | keep (money path) | Cited by `tests/broker/v2panel/test_panel_projection.py`. Not diffed; see Not reviewed. |
| `docs/references/alpaca-regulatory-fees.md` | keep | Holds the rate table pinned 2026-09-07 and the one open charging-model question. Cited by `manifest.json:1585` and the hashed FEE-001 attribution. |
| `docs/references/alpaca-fee-attribution.md` | keep | Holds the largest-remainder cent allocation policy (#2540). `golden/alpaca-fee-attribution/` has only `cases.json` and a README. Linked from ADR 0059 and `math-sources-of-truth.md` (both link-checked). |
| `docs/references/alpaca-order-fill-latency.md` | keep | The only justification for the `abs=1e-12` tolerance: it admits only the float form of an exact millisecond difference (section "Millisecond boundary and tolerance"). |
| `docs/references/alpaca-clerk-synthetic-polygon-composition.md` | keep (money path) | Minute-to-5-second composition tolerance. Linked from `math-sources-of-truth.md`. Not diffed; see Not reviewed. |
| `docs/references/synthetic-broker-position-projection.md` | keep (money path) | Cited by `app/broker/alpaca/clerk/synthesized_orders.py`. Not diffed; see Not reviewed. |
| `docs/references/custody-budget-money.md` | keep (money path) | Linked from `engine-authority-map.md` and `math-sources-of-truth.md` (both link-checked) and from `CONTEXT.md`. |
| `docs/references/replay-determinism.md` | cut | No inbound links. Its only validation, `Backend.Tests/Unit/Services/ReplayDeterminismTests.cs`, does not exist. The content is generic determinism advice. |
| `docs/references/portfolio-reconciliation.md` | cut | 11-line stub that restates the `math-sources-of-truth.md` § Portfolio/valuation row. No inbound links. |
| `docs/references/portfolio-valuation.md` | cut | Stub restating the registry row. No inbound links. |
| `docs/references/fifo-accounting.md` | cut | Restates `math-sources-of-truth.md:232`. If that row lacks the ".NET-resident because lots are EF-tracked rows" reason, move that one line there. Only inbound link is `portfolio-valuation.md:12`, which is also cut. |
| `docs/references/trade-divergence.md` | cut | Its canonical file `app/research/divergence/analysis/trade_divergence.py` no longer exists (the directory holds `bar_divergence.py`, `run_trades.py`). Its cross-reference `trade_comparison.py` was deleted in `2cbb1a0e`. No inbound links. |
| `docs/references/bouchaud-farmer-lillo-2008-market-impact.md` | cut (dead) | A port plan for four modules that were never written (`execution_cost.py`, `optimal_schedule.py`, `microstructure/order_flow.py`, `spread_impact.py`, all absent with no deletion history). Its fixture `BFL-2008-MICRO-001` is `status: planned`, and its canonical `app/research/microstructure/benchmarks.py` is absent. |

## Rows — Signal Program promotion notes

Each fixture `golden/<program>-signal/v1/attribution.md` already states the two facts these notes carry: the trace root is a regression pin, not cross-engine equivalence, and tolerance does not apply. The registry's `numerical_provenance` repeats both. The "Pinned by" sections list the parity test.

| Path | Verdict | Evidence |
|---|---|---|
| `docs/references/spy-strategy-a-signal.md` | cut | Restates `golden/spy-strategy-a-signal/v1/attribution.md` ("Reference: no LEAN or TradingView reconciliation…"; "Tolerance: not applicable"). No inbound links. |
| `docs/references/spy-strategy-b-signal.md` | cut | Same template and same restatement. No inbound links. |
| `docs/references/spy-strategy-c-signal.md` | cut | Same. No inbound links. |
| `docs/references/rsi-mean-reversion-signal.md` | cut | Same. Its only "inbound" hit is a canned message string in `alpaca-deploy-workflow.component.spec.ts:69`, not a link or a hash. |
| `docs/references/deployment-validation-signal.md` | cut | Says its two program changes are "recorded in the module and method docstrings of the canonical implementation". Everything else is template. |
| `docs/references/ema-signal-session.md` | cut | Restates `golden/ema-signal-session/v1/attribution.md` and lists `test_ema_signal_program.py`. The real LEAN evidence is the reconciliation, which is kept. |
| `docs/references/sma-crossover-signal.md` | cut, move one fact | Move the #1736 argument (the level exit is trace-equivalent on the qualified corpus and diverges only on a refused-exit retry, root `b0a136f7…` unchanged) into the comment at `app/engine/strategy/algorithms/sma_crossover.py:219`. |
| `docs/references/deployment-validation-consecutive-green.md` | keep | Holds the half-day cutoff contract (#1672). Cited by `deployment_validation.py`, `registry.py`, `golden/deployment-validation-session-window/attribution.md` and four tests. |

## Rows — design notes for research features (not ports, no fixtures)

| Path | Verdict | Evidence |
|---|---|---|
| `docs/references/baselines.md` | keep, slim | The only statement of the null-baseline methods and the buy-and-hold tautology. Linked from `engine-authority-map.md`, `research-artifact-seam.md`, `math-sources-of-truth.md`. Drop "v1 deferred" and "Upgrade path" (plan text). |
| `docs/references/monte-carlo.md` | keep, slim | The only statement of the trade-path methods and their aggregation. Drop "What's NOT in Phase D" and "Upgrade path". |
| `docs/references/walk-forward.md` | slim | Keep the fold-boundary and compounded-OOS semantics. Drop "SPY EMA normalized-gap protocol — retired" and "Upgrade path". Cited by `app/research/walk_forward/{__init__,metrics,runner}.py`. |
| `docs/references/walk-forward-study.md` | keep | Holds the frozen-verdict decisions. Cited by ADR 0056 and `walk_forward_study/verdict.py`. |
| `docs/references/grid-search.md` | keep | Holds the cell-identity and receipt decisions. Cited by ADR 0056. |
| `docs/references/run-ledger.md` | keep | Holds the hashing exclusions and `data_snapshot_id`. Cited by six code files (`app/research/runs/*.py`, `strategy-runs.component.ts`). |
| `docs/references/run-replay-proof.md` | keep | Holds the divergence classification and its known bounds. Cited by ADR 0043. |
| `docs/references/paper-live-decision-comparison.md` | keep | Linked from `engine-authority-map.md` and `math-sources-of-truth.md` (both link-checked). |
| `docs/references/return-distribution.md` | keep | The only statement of the return kinds, session segments and statistics. Linked from `engine-authority-map.md` and `CONTEXT.md`. |

## Rows — broker, clerk, data-lake and feed references (non-math, doc-read rule)

| Path | Verdict | Evidence |
|---|---|---|
| `docs/references/alpaca-credential-slots.md` | keep | Linked from ADR 0060 and `docs/runbooks/first-time-setup.md` (both link-checked). Cited by `scripts/manage_broker_configuration.py:22`. |
| `docs/references/alpaca-extended-hours.md` | keep | Linked from ADR 0060 and `math-sources-of-truth.md` (both link-checked). Cited by `broker.py`, `recovery_reduction.py`, `marketable_limit.py`. |
| `docs/references/alpaca-live-envelope.md` | keep | Cited by `manifest.json:1719` and the hashed PNL-001 attribution, by `clerk/live_envelope.py`, and linked from `engine-authority-map.md` (link-checked). |
| `docs/references/alpaca-live-arming.md` | slim | Keep "What remains (#2629)": the history readers in `clerk/live_arming.py`, and version-1 accounts refusing with `BUDGETS_NOT_SWITCHED_ON`. The ceremony, gate, refresh and seal it describes are deleted; `live_arming_ceremony.py` went in `c922122d` (#2547). |
| `docs/references/alpaca-shadow-authority.md` | slim | The receipt, session journal and twin reconciliation (`shadow_receipt.py`, `shadow_sessions.py`, `services/alpaca_shadow_reconciliation.py`) were deleted in `c922122d`. `shadow_authority.py`, `shadow_broker.py` and `shadow_activation.py` remain. Keep worlds, paths, fill models and cold start. Drop the retired sections. |
| `docs/references/alpaca-live-authority.md` | keep (money path) | Graduation is still live (`LiveGraduationComponent`). Whether "The thirteen gates, re-meant" are all still live was not checked; see Not reviewed. |
| `docs/references/alpaca-sqlite-clerk-invariant-traceability.md` | slim | Drop the rows naming tests that no longer exist (for example `tests/broker/alpaca/clerk/test_stream_health.py`). Linked from `doc-authority.md` and `docs/runbooks/alpaca-sqlite-clerk-recovery-and-cutover.md` (link-checked). |
| `docs/references/alpaca-sqlite-clerk-lease-heartbeat-cadence.md` | keep | Holds "Why 3× and not a golden fixture". Cited by `clerk/sqlite/reconciliation_sweep.py`. |
| `docs/references/alpaca-sqlite-clerk-recovery-language.md` | keep | Cited by `clerk/sqlite/recovery_policy.py`. Indexed in `doc-authority.md:198`. |
| `docs/references/alpaca-sqlite-clerk-source-guarantees.md` | keep | Official-source provenance for the adapter constraints. Indexed in `doc-authority.md:199`. |
| `docs/references/clerk-invariants.md` | keep | Holds the custody tolerance contracts (EXIT quantity, fill delta pricing, position drift). Cited by five clerk modules and two tests. |
| `docs/references/feed-reconnect-continuity.md` | keep | Cited by ADR 0053 and `docs/broker-clerk-fleet-authority.md`. This is the IBKR read-only feed, which is sacred. |
| `docs/references/ibkr-history-resume-fill.md` | keep | Cited by ADR 0053 and `app/config.py`. IBKR read-only feed, sacred. |
| `docs/references/lake-adjustment-dimension.md` | keep | Linked from `engine-authority-map.md` and `math-sources-of-truth.md` (both link-checked). |
| `docs/references/lake-committed-admission.md` | keep | Linked from `engine-authority-map.md` and `math-sources-of-truth.md` (both link-checked). |
| `docs/references/polygon-throttle.md` | keep | Cited by `app/config.py` and `app/services/polygon_client.py`. |

## Rows — one-off research answers, spikes, runbooks

| Path | Verdict | Evidence |
|---|---|---|
| `docs/references/final-bar-decisions-2467.md` | slim | Linked from `docs/known-gaps.md` (link-checked). Keep "The answer" and "Recommendation". Drop "Dead machinery found", "Follow-ups" and "Method and reproduction". It names four files deleted in `457787e0`. |
| `docs/references/final-bar-decisions-2467.json` | cut | A 3,476-line raw measurement dump for a one-off answer. Its only inbound links are its own `.md` (`:65`, `:173`, `:266`), all in the sections the slim drops. |
| `docs/references/ema-verdict-under-live-terms.md` | cut (if #2707 cuts its script) | One-off answer to #2466 that names four deleted files (`order_sizer.py`, `engine/live/config.py`, …). Its only live inbound link is its measuring script, `scripts/measure_verdict_under_live_terms.py:48`. If #2707 keeps the script, slim the note to "Answer" plus the 2026-09-30 addendum. |
| `docs/references/two-bots-one-symbol-2469.md` | slim | `tests/broker/alpaca/clerk/sqlite/test_two_bots_one_symbol.py:5` cites it as the rationale. Keep §1–§3 (fill attribution, wash trades, PDT and settlement). Drop §6 drafted follow-ups and §7 dead machinery. ADR 0009:190 now marks the guard it discusses superseded. |
| `docs/references/fill-model-parity-spike-2026-05-19.md` | slim | `app/lean_sidecar/trusted_samples/ema_crossover.py:16` cites it for the LEAN fill-model decision. Keep §1 (LEAN fill observed), §3 (decision) and §5 (session-boundary note). Drop §6–§7 and the stale `observations.cs` path. |
| `docs/references/qc-aapl-phase3-capture-runbook.md` | merge → `reconciliations/qc-aapl-phase3.md` | Same construct. The runbook is the only regeneration recipe, because `golden/qc-aapl-phase3/attribution.md` has none. Cited by `qc_reconciler.py:276` and `docs/ml-predictions-authority.md:13,431`. |
| `docs/references/quantconnect-precomputed-predictions.md` | slim | Keep "Tolerances", "Pinned decisions" and the fixture provenance. Drop the "§B / §C runbook — step by step" (done) and "Future steps". Cited by `golden/qc-precomputed-predictions/README.md` and `ml-predictions-authority.md`. |
| `docs/references/spy-vwap-reversion-port.md` | cut (dead, with #2704) | `SpyVwapReversionAlgorithm` is not among the registrations in `app/engine/strategy/registry.py` (ema, sma, rsi, deployment_validation, spy_strategy_a/b/c), and nothing in `app/`, `Frontend/` or `Backend/` names it outside its own module. |

## Rows — reconciliation reports

| Path | Verdict | Evidence |
|---|---|---|
| `reconciliations/ema-crossover-signal-lean-2026-07-18.md` | keep | The one real cross-engine receipt. **Never cut it**: `app/data/strategy_validation_manifest.json:37,118` carries it as `reconciliation_ref`, and it is cited by six code files, the OpenAPI snapshot and `golden/ema-signal-session/v1/attribution.md`. |
| `reconciliations/rsi-mean-reversion-lean-2026-09-01.md` | keep | Holds the accepted divergence "median duration convention". Cited by `manifest.json:1541` and the hashed ENG-009 attribution. |
| `reconciliations/qc-aapl-phase3.md` | keep (merge target) | Holds "Tolerances accepted (and why)". Cited by `research/parity/qc_reconciler.py` and `tests/research/parity/test_qc_aapl_phase3_trade_parity.py`. |
| `reconciliations/data-lab-spy-2026-04-17-to-2026-04-24.md` | keep, slim | The only acceptance record for Data Lab OHLCV, indicators and the options companion; none has a golden fixture. Cited by `app/volatility/solver.py`, `tests/services/test_bs_greeks.py`, `tests/volatility/test_solver.py`. Drop "Suggested follow-up tickets". |
| `reconciliations/engine-lab-runs-75-76-statistics-validation-plan.md` | slim | 1,619 lines, mostly plan. Keep "Executive conclusion" (`:28`), "Product decisions" (`:71`, cited by § from `run_verdict_service.py:7`), "Reconciliation findings" (`:893`) and "Tolerance policy" (`:1316`). Drop `:409–892`, `:1024–1315` and `:1338–1596`. It names six test files that do not exist. |
| `reconciliations/cross-engine-w12mo-2026-06-10.md` | cut, move one fact | No inbound links. The four W12mo cells are pinned by `tests/research/parity/test_cross_engine_study.py`. Move one sentence, that DIA was attempted, blocked and deferred, into `golden/cross-engine-studies/README.md`, which does not mention DIA. |
| `reconciliations/lean-vs-python-spy-ema-6day-2026-06-10.md` | cut | A zero-fill smoke run of an endpoint ("lean_total_fills": 0) against `SpyEmaCrossoverAlgorithm`, deleted in `c35884b6`. Inbound links only from notes in this list. |
| `reconciliations/data-lake-flag-flip-2026-08-28.md` | cut | The flag flip is done: `app/config.py:311` speaks of the "DATA_LAKE_ENABLED flag that used to select between them". Its one accepted divergence (no `alternative/interest-rate` subtree) already lives in `app/lean_sidecar/lake_mount.py`'s docstring. No inbound links. |
| `reconciliations/dry-run-2026-05-09/day-0.md` | cut | A generated output from an IBKR-era dry run, with hashes of parquet files that are not in the repo. The test hit (`tests/engine/live/test_live_artifact_io.py:48`) writes a temporary file named `day-0.md`; it does not link this note. |
| `reconciliations/spy-vwap-reversion.md` | cut (dead, with #2704) | Accepted fill-price drift for the dead VWAP port above. Linked from `tests/integration/reconciliation/test_spy_vwap_reversion_qc.py:53`, which is cut with the port. |

## Rows — `docs/references/golden-fixtures/` (30 files)

Every note here restates its fixture's `attribution.md` (formula, oracle, tolerance, grid, regeneration) and adds nothing the attribution or the parity test lacks. The copies have also drifted:

- **ENG-003** says `atol=1e-9`, but `manifest.json` (ENG-003 `tolerance.atol: 1e-12`) and `ENG-003/v1/attribution.md:31` say `1e-12`.
- **IV-001..004** are filed under `implied-volatility/`, but the fixtures live in `options-pricing/`.
- The **README** lists a `volatility/` category that does not exist, and says "Files are created alongside the fixture in PR #2".

| Path | Verdict | Evidence |
|---|---|---|
| `golden-fixtures/README.md` | cut | Describes the directory as a "PR #1 foundation" placeholder. It names a nonexistent `volatility/` category. |
| `golden-fixtures/engine-statistics/ENG-001.md`, `ENG-001b.md`, `ENG-002.md`, `ENG-004.md`, `ENG-005.md` | cut | Same tolerances as their attributions. The words found only in the note are boilerplate ("Status", "Category", "Pinned"). |
| `golden-fixtures/engine-statistics/ENG-003.md` | cut | Wrong tolerance (`1e-9` vs `1e-12`, see above). Proof that the copy drifts. |
| `golden-fixtures/implied-volatility/IV-001.md` … `IV-004.md` | cut | Restate `options-pricing/IV-00x/v1/attribution.md`. They are filed under a path that does not exist in the fixtures tree. |
| `golden-fixtures/indicator-reliability/REL-001.md`, `REL-004.md` | cut | Restate their attributions. The note-only words are prose. |
| `golden-fixtures/indicators/IND-001.md` … `IND-003.md` | cut | Restate their attributions. All tolerances are `1e-9`. |
| `golden-fixtures/options-pricing/BS-001.md` … `BS-007.md` | cut | Restate their attributions (BS-001 compared in full; the same QuantLib cross-check and grid). |
| `golden-fixtures/options-pricing/OPT-IB-002.md` | cut, move one fact | Move the mid-price fingerprint: inverting the bid/ask mid instead of `modelGreeks.optPrice` shows calls off by about 0.06 vol and puts by about 0.002, the asymmetry coming from IBKR's discrete-dividend handling. Put it in the docstring of `tests/fixtures/test_ibkr_iv_fixtures.py`. The attribution already says mid is wrong. The test already pins the round-trip and `_MAX_INTRINSIC_VIOLATIONS = 15`. |
| `golden-fixtures/realized-volatility/RV-001.md` … `RV-004.md` | cut | Restate their attributions. RV-003's ET-midnight basis is in `iv-rv-basis-alignment.md` (kept). |
| `golden-fixtures/research-primitives/RP-001.md` … `RP-004.md` | cut | Restate their attributions. RP-004's `ddof=1` fact is in `RP-004/v1/attribution.md:10`. |

## Rows — the two top-level math docs

| Path | Verdict | Evidence |
|---|---|---|
| `docs/math-sources-of-truth.md` | keep, edit rows | Protected-canonical (`scripts/check_documentation_contract.py:37`). Linked from CLAUDE.md, a rule, two skills and the served UI asset. Edits ride with the cuts: re-point every Reference or Validated cell that names a cut note (lines 135–137, 149, 167, 171, 173, 174, 214, plus the § Portfolio and § divergence rows). Rows that name deleted canonicals, such as `trade_divergence.py`, go with whichever ticket cuts the code. |
| `docs/math-rigor.md` | slim | An unfinished 10-item upgrade plan. Code provenance blocks cite only Upgrade 1 (variance-time interpolation, and the known bias of the current linear-in-σ `iv_builder.py`, a documented non-equivalence) and Upgrade 4 (FRED tenors): `iv_builder.py:4,6`, `iv30_health.py:4`, `volatility/surface.py:6`, `services/fred_service.py:4`, `math-sources-of-truth.md:152,262,342`. Keep those two sections' "Problem" and "Correct form". Drop Upgrades 2–3 and 5–10, which nothing cites. |

## What moves where (slim and merge carry-overs)

| Fact | From | To |
|---|---|---|
| The RSI Data Lab path has no 3×length warm-up mask (re-check against #2611 first) | `rsi.md` | `pandas-ta-dispatch.md` RSI entry |
| Polygon bar `vwap` ≠ session VWAP | `vwap.md` | `pandas-ta-dispatch.md` VWAP entry |
| RMA is covered only through the ADX/Supertrend recursion | `rma.md` | `pandas-ta-dispatch.md` RMA entry |
| The #1736 level exit is trace-equivalent on the corpus | `sma-crossover-signal.md` | comment at `sma_crossover.py:219` |
| IBKR mid-vs-optPrice divergence fingerprint | `golden-fixtures/options-pricing/OPT-IB-002.md` | `tests/fixtures/test_ibkr_iv_fixtures.py` docstring |
| DIA attempted, blocked, deferred | `reconciliations/cross-engine-w12mo-2026-06-10.md` | `golden/cross-engine-studies/README.md` |
| QC AAPL capture and regeneration recipe | `qc-aapl-phase3-capture-runbook.md` | `reconciliations/qc-aapl-phase3.md` |
| FIFO is .NET-resident because lots are EF-tracked rows (only if missing) | `fifo-accounting.md` | `math-sources-of-truth.md:232` status cell |

## Links each cut breaks

Unless noted, each link below is re-pointed or dropped in the cutting PR.

- **Link-checked, so CI fails if left in place** (`scripts/check_documentation_contract.py` validates links from ADRs, PRDs, runbooks and the canonical list): `docs/math-sources-of-truth.md:135,136,137,149,171,173,174` link golden-fixtures notes. Every other kept canonical link points at a note that stays.
- **Sacred fixture files linking a cut note.** Text-only link edits; none of these attributions is hashed in `manifest.json`: `app/engine/tests/fixtures/golden/macd_12_26_9/attribution.md:31`, `supertrend_10_3/attribution.md:28`, `adx_14/regenerate.py:13`.
- **Code and test docstrings:**
  - `app/services/bs_greeks.py:31` (options-bs-greeks)
  - `app/engine/tests/test_adx.py:241` (adx)
  - `app/engine/strategy/algorithms/sma_crossover.py:224` (sma-crossover-signal)
  - `app/research/parity/qc_reconciler.py:276` (capture runbook, which becomes the merged path)
  - `scripts/measure_verdict_under_live_terms.py:48` (ema-verdict)
  - `app/engine/strategy/algorithms/spy_vwap_reversion.py:18` and `tests/integration/reconciliation/test_spy_vwap_reversion_qc.py:53` (both go with the dead port)
- **Supporting docs, not link-checked:**
  - `docs/architecture/lean-sidecar-lab.md:5,585,592` (lean-engine)
  - `docs/process/pr-review-escalations.md:122` (OPT-IB-002)
  - `docs/ml-predictions-authority.md:13,431` (runbook, which becomes the merged path)
  - `references/arxiv-0809.0822v1/attribution.md:27` (bouchaud; vendored)
  - `references/quantconnect/spy_vwap_reversion/main.py:39,54` (vwap port; vendored)
  - `pandas-ta-dispatch.md:30-31` (indicator notes)
  - `deployment-validation-consecutive-green.md:90` (deployment-validation-signal)

## Hazards for the cutting PR

1. **Run the link contract.** Run `pytest PythonDataService/tests/contracts/test_documentation_contract.py` after the edits. The golden-fixtures cut breaks it unless `math-sources-of-truth.md` is re-pointed in the same commit.
2. **Keep the notes that sealed files cite.** Do not cut `strategy-abc-self-equivalence.md`, `rsi-mean-reversion-lean-2026-09-01.md`, `alpaca-regulatory-fees.md` or `alpaca-live-envelope.md`. They are cited from hashed attributions (ENG-008, ENG-009, FEE-001, PNL-001 `file_sha256`) and from `manifest.json` citations (`:1453,1497,1541,1585,1719`). Cutting any of them forces a sealed-fixture regeneration.
3. **The strategy validation manifest.** `app/data/strategy_validation_manifest.json:37,118` carries `docs/references/reconciliations/ema-crossover-signal-lean-2026-07-18.md` as `reconciliation_ref`, which surfaces in strategy validation and the deploy catalog. Never move or rename that file.
4. **Dead-port cuts wait on #2704.** The two VWAP rows and the bouchaud row are dead-code cuts. Land them with the engine and research dead-code PRs (#2704, #2705), not before, so each PR re-checks reachability at its own SHA.
5. **Skills flag missing notes.** `auto-research-tick` reports a missing `docs/references/<name>.md` as a P2 finding (`.claude/skills/auto-research-tick/SKILL.md:217,223,306-307`), and `port-indicator` and `extract-math-from-paper` still *create* a note per port. `.claude/skills/` is write-protected in the agent sandbox, so #2715 has to land, or these cut notes regrow and get flagged.
6. **Slim edits must not touch hashed attribution text.** For every "move" above, the destination was chosen to avoid hashed attributions.
7. **Re-check at the cutting SHA.** Kill lists age.

## Pointers outside this area

- **#2715 (rules, skills):** the paperwork rule still demands a note per port and per reconciliation: `.claude/rules/numerical-rigor.md:74,97,140`, `.claude/rules/testing.md:121`, `.claude/skills/port-indicator/SKILL.md:69-70,80`, `extract-math-from-paper/SKILL.md:70`, `reconcile-backtest/SKILL.md:76`. `learn-ai-validation/SKILL.md:155` still says the notes directory is "currently empty".
- **#2704 (engine dead code):** `app/engine/strategy/algorithms/spy_vwap_reversion.py`, `tests/engine/strategy/test_spy_vwap_reversion.py`, `tests/integration/reconciliation/test_spy_vwap_reversion_qc.py`, `golden/spy-vwap-reversion-qc/`, `references/quantconnect/spy_vwap_reversion/`.
- **#2705 (research, small packages):** the planned microstructure fixture `golden/microstructure/` (`BFL-2008-MICRO-001`, no code) and `references/arxiv-0809.0822v1/`.
- **#2707 (scripts):** `scripts/measure_verdict_under_live_terms.py`, which decides whether `ema-verdict-under-live-terms.md` goes or slims.
- **#2710 (Backend):** whether `PositionEngine.cs`, `PortfolioValuationService.cs` and `PortfolioReconciliationService.cs` are live. Their registry rows stay or go with that code.
- **#2711 (one-off docs):** `docs/design/bouchaud-farmer-lillo-2008-microstructure-implementation-design.md`, `docs/process/pr-review-escalations.md`, and the superpowers plans and specs that link the alpaca-* notes (they inflate those notes' inbound counts but are sediment).
- **#2712 (architecture docs):** `docs/architecture/lean-sidecar-lab.md` links `lean-engine.md` three times.

## Not reviewed

Every file in the area has a verdict, but some verdicts rest only on headings, inbound links and liveness checks. The note's body was **not** compared line by line against its fixture attribution or test. They are provisional keeps (money path: keep when unsure), and their slimming is unassessed:

- **Money path and broker:** `broker-v2-fifo-pnl.md` (vs `golden/broker-v2-fifo-pnl/`), `broker-v2-readiness-summary.md`, `alpaca-clerk-synthetic-polygon-composition.md`, `synthetic-broker-position-projection.md`, `custody-budget-money.md`, `clerk-invariants.md`, `alpaca-live-envelope.md` (vs the PNL-001 attribution), `alpaca-live-authority.md` (whether each of "the thirteen gates" is still live), `alpaca-credential-slots.md` ("Retired, and never retired" section), `alpaca-extended-hours.md`.
- **Math:** `lean-native-statistics-oracle-v1.md` (vs `golden/lean-statistics-oracle-v1/`), `realized-equity-staircase-v1.md`, `engine-validation-analytics.md`, `alpaca-fee-attribution.md` (vs `golden/alpaca-fee-attribution/cases.json`).
- **Research features:** `baselines.md`, `monte-carlo.md`, `walk-forward-study.md`, `grid-search.md`, `run-ledger.md`, `run-replay-proof.md`, `paper-live-decision-comparison.md`, `return-distribution.md`. Their code modules exist, but reachability belongs to #2705.
- **Data plane and feed:** `lake-adjustment-dimension.md`, `lake-committed-admission.md`, `polygon-throttle.md`, `feed-reconnect-continuity.md`, `ibkr-history-resume-fill.md`, `alpaca-sqlite-clerk-*`.
- **`docs/math-sources-of-truth.md` rows:** not audited row by row for deleted canonicals beyond the ones named above.
