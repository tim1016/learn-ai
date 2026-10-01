# Comment citations: rule-file citations (#2746)

Part of map #2700. Read at `origin/master` **`6a4d7d396108ef16471d8df888b9ded74d3c2892`**. This is a plan only; nothing is edited from this ticket.

**Question.** The owner ruled that comments cite ADRs only (◆). Which ADR should each comment that cites a rule file (`CLAUDE.md`, `.claude/rules/*`, `AGENTS.md`) point to? `docs/math-sources-of-truth.md` is cut (◆), so its citations get the same treatment.

## How this was judged

- **Census.** `git grep -nE "CLAUDE\.md|AGENTS\.md|\.claude/rules/|guiding philosophy|numerical-rigor|temporal-rigor|math-sources-of-truth" -- PythonDataService scripts Backend Backend.Tests Frontend/src ':!*.md'` found 264 lines. I widened it twice:
  - `philosophy #`, `guiding-philosophy`, `testing.md`, `python.md`, `angular.md`, `learn-ai-validation` and `single-canonical-implementation`. These added 13 citations that name a rule without a file path, such as `decision_receipts.py:186` ("philosophy #5") and `gallery.types.ts:12`.
  - `temporal rigor`, `repo rule` and `project rule`. These added 5 more, such as `routers/jobs.py:9` ("the project rule: Python owns all math") and `return_distribution.py:14` ("per repo rules").
  - Plain prose such as "single source of truth" with no rule named (about 35 lines) is **not** a citation and gets no row.
- A scratch tokenizer classified every hit as a `#` comment, a docstring, a string literal or code, and printed its comment block. Each row was judged from that block.
- **Verdicts.**
  - **drop.** The comment already states the reason, or the reason is weak. This covers the coding hard rules (no silent catch, don't duplicate helpers, Angular test style), and a field merely labelled "`int64 ms UTC` per temporal-rigor".
  - **re-point → ADR NNNN.** An ADR in force records the decision. I read the clause for each target: ADR 0022 (a)–(e), ADR 0035 D3/D8/D12, its crown-jewel invariants (`0035:222-227`) and its annex (`0035:333-340`), ADR 0053 §14/§15 and ADR 0058 D6.
  - **re-point → E5 / E6 / E7.** These are entries 5, 6 and 7 of the [merged ADRs-owed list](https://github.com/tim1016/learn-ai/blob/research/lean-adrs-owed/docs/research/lean-and-mean/adrs-owed.md). E5 is "Python owns canonical math", E6 is "Numerical rigor" and E7 is "Amend ADR 0022". These ADRs are not written yet, so the rows wait for them (hazard H6).
  - **ADR owed (E7+a / E7+b).** Two decisions that no ADR in force and no merged entry records. I propose both as additions to entry 7; see [ADRs owed beyond the merged list](#adrs-owed-beyond-the-merged-list).
  - **goes with dead code.** The citation sits in code that a dead-code list (#2701–#2710) or a closed test list already cuts. It is not judged further.
- **How the rule maps onto targets.** I followed the merged list's table "where rule-file citations go":
  - `temporal-rigor.md`:
    - It re-points to ADR 0022 when the rule is the *reason* for a choice that would otherwise look arbitrary: an ET anchor, a value derived from the calendar, the one calendar module, the liveness split, or the display component.
    - It drops when the comment only labels a field as `int64 ms UTC`, or already states its own reason (a DST shift, `new Date(string)`, "no later instant exists").
  - `numerical-rigor.md`:
    - It re-points to E6 for the taxonomy, tolerance defaults, equivalence levels and fixture lifecycle.
    - Citations that use `numerical-rigor.md` for **timestamps** point at a section ADR 0022 moved out in 2026-07 (`0022:22`). They are treated as time citations.
  - `CLAUDE.md` #5, "single source of truth", and `AGENTS.md` "Python owns all math":
    - They re-point to E5 when the duplicate or the authority is **math**.
    - Non-math "one canonical helper" citations drop. The comment always states the reason, and E5 is scoped to math concepts (see [For the map](#for-the-map), question 1).
- **Tags.** **hashed**: the file is a Signal Program build-proof source (`app/engine/strategy/program_sources.py`). **codegen**: the text flows into `contracts/openapi/python-data-service.openapi.json` and `Frontend/src/app/api/broker.types.ts`. I found these by matching each docstring or string line against `broker.types.ts`. **money**: a clerk, Alpaca or fleet money-path file. **feed**: the sacred IBKR read-only feed.
- **Earlier verdicts kept.** `math-sources-of-truth.md` citations were already judged as doc citations by #2742 (app) and #2743 (rest). The cut changes none of those verdicts. Their rows are repeated here, marked "as #2742" or "as #2743", so this list covers everything.

## Summary

- **246 citation rows** across code, comments and tests.
  - **114 drop.**
  - **94 re-point.** 46 go to ADRs in force (0022 ×30, 0035 ×11, 0053 ×4, 0058 ×1). 48 go to merged entries (E5 ×15, E6 ×25, E7 ×8).
  - **14 ADR owed.** E7+a (finite ingestion fails fast) ×12 and E7+b (the data-lake noon-UTC anchor) ×2.
  - **24 go with dead code.**
- **Hashed:** 2 rows (`normalized_gap.py:4`, `utils/timestamps.py:3`). **Codegen:** 9 rows. **Money:** 42. **Feed:** 10.
- **Not comments:** 9 generated `broker.types.ts` lines, 3 emitted fixture-attribution strings, 1 manifest note, Frontend test data, and 3 tests or checkers that read rule files. These are [listed, not judged](#not-comments-listed-not-judged).
- **Why most drop.** Most citations restate the rule they cite, such as "`int64 ms UTC` per temporal-rigor" or "don't duplicate". About 17 cite `numerical-rigor.md` for timestamp labels and 11 more for fail-fast ingestion. ADR 0022 moved both sections to `temporal-rigor.md` in 2026-07.

## Rows

### `PythonDataService/app/` — broker, Alpaca clerk, fleet, IBKR, broker configuration

| `file:line` | Rule cited | Verdict | Target | Reason |
|---|---|---|---|---|
| `broker/alpaca/active_binding.py:57` | CLAUDE.md hard rule (backend-authored copy) | **re-point** · money | ADR 0035 D12 | The prose beside a refusal code is backend-authored because "the frontend derives no safety" (merged table). |
| `broker/alpaca/adapter.py:7` | temporal-rigor (ingestion boundary) | **drop** · money | — | The bullet states the one RFC-3339 → ms conversion itself. |
| `broker/alpaca/adapter.py:184` | temporal-rigor (`date-et`) | **re-point** · money | ADR 0022 (a, e) | A settlement date is anchored at the ET day so `date-et` display never drifts: the date anchor and the display modes. |
| `broker/alpaca/clerk/fills.py:21` | temporal-rigor live-subscription relaxation | **re-point** · money | ADR 0035 D3 + invariants | Dedup on `(account_id, event_key)` is ADR 0035's content-addressed idempotency and its "live-idempotent websocket dedup" invariant. The temporal-rigor relaxation is about IBKR bars. |
| `broker/alpaca/clerk/fills.py:28` | same (`Reference:` line) | **re-point** · money | ADR 0035 D3 + invariants | Same. |
| `broker/alpaca/clerk/fills.py:164` | same | **re-point** · money | ADR 0035 D3 + invariants | Same. |
| `broker/alpaca/clerk/live_authority.py:93` | "repo philosophy #5" | **drop** · money | — | It states the reason itself ("a fourth spelling is a fourth thing to keep in step"). A mode table is not math. |
| `broker/alpaca/clerk/recovery_reduction.py:835` | AGENTS.md "Python owns all math" | **re-point** · money | E5 | Flatten reach and cost are computed only by the Clerk; the browser renders them (merged list, re-targeted from ADR 0031). |
| `broker/alpaca/clerk/sqlite/decision_receipts.py:185` | CLAUDE.md #5 | **drop** · money | — | "So the three shapes never drift apart" is the reason. One helper for one row shape is code structure. |
| `broker/alpaca/clerk/sqlite/decision_receipts.py:186` | same sentence | **drop** · money | — | Same edit. |
| `broker/alpaca/clerk/sqlite/facts.py:330` | numerical-rigor "hash-chained-schema rule" | **re-point** · money | ADR 0035 D8 | The rule file has no such section. Old rows must stay byte-identical because the log is hash-chained (D8). |
| `broker/alpaca/clerk/sqlite/facts.py:1231` | CLAUDE.md #5 | **drop** · money | — | "Exactly one payload rule" is stated. |
| `broker/alpaca/clerk/sqlite/order_evidence.py:8` | CLAUDE.md #5 | **drop** · money | — | "This one gate rather than each keeping its own copy" is stated. |
| `broker/alpaca/clerk/sqlite/order_evidence.py:103` | numerical-rigor (`FILL_PRICE_DRIFT` $0.01) | **re-point** · money | E6 | The $0.01/share basis is the taxonomy default (merged list). Its `clerk-invariants.md` half is entry 2's row (#2742 B). |
| `broker/alpaca/clerk/sqlite/schema.py:10` | "Guiding-philosophy #5" | **re-point** · money | ADR 0035 annex | ADR 0035 binds the pinned-contracts doc as its annex (`0035:333-340`), and the parity test enforces the pair. This is a module docstring, outside `SCHEMA_DDL`. |
| `broker/alpaca/clerk/synthetic_activation.py:104` | temporal-rigor admissible range | **re-point** · money | E7 | `MAX_TIMESTAMP_MS` as the schema bound is entry 7's range clause. |
| `broker/alpaca/marketable_limit.py:22` | CLAUDE.md #5 | **drop** · money | — | The paragraph states why the tick rule exists twice and names its parity test. That is all #5 would add. |
| `broker/alpaca/profile/runtime_context.py:78` | CLAUDE.md #5 | **drop** · money | — | It names the parity test. |
| `broker/alpaca/trade_updates.py:33` | temporal-rigor `live_idempotent` | **re-point** · money | ADR 0035 invariants | Websocket redelivery dedup is an ADR 0035 crown-jewel invariant (`0035:227`). |
| `broker/alpaca/trade_updates.py:284` | temporal-rigor | **drop** · money | — | It states why the key uses canonical ms: both the socket path and the REST path build the same key. |
| `broker/alpaca/trade_updates.py:658` | temporal-rigor (surface, never drop) | **re-point** · money | ADR 0035 invariants | The same dedup invariant. The surface-and-count step is stated. |
| `broker/contract/models.py:11` | temporal-rigor | **drop** | — | A label. The paragraph states the one adapter boundary. |
| `broker/contract/models.py:16` | numerical-rigor "Decimal discipline" | **drop** | — | It states why `float` is fine here (display-only broker figures). The rule has no such section. |
| `broker/contract/models.py:353` | temporal-rigor (calendar vs live) | **re-point** · codegen | ADR 0022 (b, c) | `BrokerClockEvidence` is ADR 0022's scheduled-structure and liveness split. |
| `broker/fleet/drain_deadline.py:21` | temporal-rigor (no session literal) | **re-point** · money | ADR 0022 (b) | The second leg derives from the calendar because no hardcoded session times are allowed. |
| `broker/fleet/internal_http.py:112` | temporal-rigor (scope) | **re-point** | ADR 0022 | ADR 0022 governs "two things and only two things". A local monotonic deadline is outside both. |
| `broker/ibkr/bar_models.py:9` | numerical-rigor (timestamps) | **drop** · feed | — | A label, and the wrong rule. |
| `broker/ibkr/capability.py:55` | temporal-rigor (DST via NY zone) | **drop** · feed | — | It states the bug that a fixed offset causes. |
| `broker/ibkr/client.py:281` | numerical-rigor "surfaced, never silenced" | **drop** · feed | — | It states why the flag exists. The phrase is temporal-rigor's, not numerical-rigor's. |
| `broker/ibkr/minute_assembler.py:26` | numerical-rigor (finite ingestion is strict) | **ADR owed** · feed | E7+a | Finite ingestion fails fast. No ADR records that half; the live half is ADR 0053 §14. |
| `broker/ibkr/minute_assembler.py:579` | temporal-rigor live relaxation | **re-point** · feed | ADR 0053 §14 | §14 records exactly this: a post-emit correction is ignored and counted. |
| `broker/ibkr/models.py:4` | numerical-rigor (timestamps) | **re-point** | ADR 0022 (a) | The same sentence as #2742's `models.py:3` re-point; the bullet that follows is the int64-ms rule. One edit. |
| `broker_configuration/legacy_environment.py:35` | CLAUDE.md #5 | **drop** | — | It names its parity test. |
| `broker_configuration/records.py:10` | temporal-rigor | **drop** | — | A label (`int64 ms UTC`, `_at_ms`). |
| `broker_configuration/runtime.py:16` | CLAUDE.md #5 | **drop** | — | It names its parity test. The reason for the duplicate is ADR 0060 D7, which that test cites. |
| `config.py:345` | `PythonDataService/CLAUDE.md` | **drop** | — | The comment states the WSL2 value itself. |

### `PythonDataService/app/` — data lake, engine, LEAN sidecar, market data, installation migration

| `file:line` | Rule cited | Verdict | Target | Reason |
|---|---|---|---|---|
| `data_lake/backfill.py:486` | temporal-rigor calendar authority | **re-point** | ADR 0022 (b, d) | Sessions come from the one canonical calendar. |
| `data_lake/bar_validation.py:4` | temporal-rigor fail-fast ingestion | **ADR owed** | E7+a | The lake-admission boundary refuses rather than repairs. |
| `data_lake/cache_import.py:468` | temporal-rigor | **goes with dead code** | — | The whole module is cut by #2705 (finished lean-cache importer). |
| `data_lake/cache_import.py:490` | temporal-rigor | **goes with dead code** | — | Same. |
| `data_lake/cache_import.py:533` | temporal-rigor | **goes with dead code** | — | Same. |
| `data_lake/run_materialization.py:442` | numerical-rigor (series alignment) | **drop** | — | The docstring states why: a holed series reports numbers that look complete. |
| `data_lake/sessions.py:5` | temporal-rigor "Calendar authority" | **re-point** | ADR 0022 (d) | One `mcal` constructor; this file is a thin adapter. |
| `data_lake/types.py:101` | temporal-rigor date anchor | **re-point** | ADR 0022 (a) | A trading date travels as its session-open instant. |
| `data_lake/types.py:425` | temporal-rigor date anchor | **re-point** | ADR 0022 (a) | Same. |
| `engine/data/lean_format.py:64` | "guiding-philosophy #5" | **drop** | — | It states the reuse. The ticker alphabet is not math. |
| `engine/data/policy_store.py:157` | "guiding-philosophy #5" | **drop** | — | Same. |
| `engine/edge/edge_score.py:87` | numerical-rigor probability tolerance | **re-point** | E6 | `atol=1e-10, rtol=0` is the rule's probability default; E6 must carry that table (H7). |
| `engine/edge/regime_clustering.py:9` | CLAUDE.md "Sovereign over the math" | **re-point** | E6 | Merged list. #2706's pointer says most of this module may be cut, which would make this row go with dead code. |
| `engine/edge/trade_simulator.py:19` | numerical-rigor (timestamps) | **drop** | — | A label, and the wrong rule. |
| `engine/live/divergence/bar_series_joiner.py:11` | numerical-rigor (no forward-fill) | **goes with dead code** | — | #2704 cuts `engine/live/divergence/`. |
| `engine/live/divergence/replay_divergence.py:52` | numerical-rigor `INDICATOR_STATE_DRIFT` default | **goes with dead code** | — | Same. |
| `engine/live/divergence/replay_divergence.py:129` | numerical-rigor (no invented value) | **goes with dead code** | — | Same. |
| `engine/results/statistics.py:434` | temporal-rigor | **drop** | — | It states the reason: no later instant exists to price the exit at. |
| `engine/strategy/normalized_gap.py:4` | `math-sources-of-truth.md` | **drop** · **hashed** | — | As #2742: an index row; the formula and the fixture are named. |
| `engine/strategy/spec/__init__.py:15` | `math-sources-of-truth.md` | **drop** | — | As #2742. |
| `engine/strategy/spec/primitives.py:86` | `math-sources-of-truth.md` | **drop** | — | As #2742. |
| `engine/strategy/spec/tests/test_spec_router.py:151` | numerical-rigor "Timestamp rigor" | **drop** | — | A label; the asserts say it. |
| `engine/strategy/spec/tests/test_spec_sma_parity.py:15` | `math-sources-of-truth.md` | **drop** | — | As #2742. |
| `engine/tests/test_lean_format_session_filter.py:10` | numerical-rigor `DECISION_MISMATCH` | **re-point** | E6 | It classifies the regression with the taxonomy. |
| `engine/tests/test_session_wrapper.py:245` | temporal-rigor | **drop** | — | It states the reason. |
| `installation_migration/records.py:16` | temporal-rigor admissible range | **re-point** | E7 | `MAX_TIMESTAMP_MS` bound. |
| `lean_sidecar/cross_reconciler.py:58` | numerical-rigor tolerances | **goes with dead code** | — | #2704; its only importer is the `cross-reconcile` route #2706 cuts. |
| `lean_sidecar/manifest.py:10` | numerical-rigor "Timestamp rigor" | **drop** | — | A label, in a section that moved out. |
| `lean_sidecar/polygon_canonical.py:206` | numerical-rigor "External-API ingestion" | **ADR owed** | E7+a | Duplicates and non-monotonic bars surface rather than being repaired. (That section now lives in temporal-rigor.) |
| `lean_sidecar/polygon_canonical.py:255` | same | **ADR owed** | E7+a | Same guards. |
| `lean_sidecar/polygon_canonical.py:327` | same | **ADR owed** | E7+a | Same, plus the strict and lenient session-completeness modes. |
| `lean_sidecar/reconciler.py:8` | numerical-rigor taxonomy | **goes with dead code** | — | #2704; it is gated on #2706's cut of `POST /runs/{run_id}/reconcile`, which that list confirms. |
| `lean_sidecar/reconciler.py:49` | numerical-rigor `commission_atol` | **goes with dead code** | — | Same. |
| `lean_sidecar/reconciler.py:224` | same | **goes with dead code** | — | Same. |
| `marketdata/feed.py:164` | temporal-rigor | **drop** | — | A label. |

### `PythonDataService/app/` — research, routers, schemas

| `file:line` | Rule cited | Verdict | Target | Reason |
|---|---|---|---|---|
| `research/ml/artifact.py:8` | numerical-rigor "Timestamp rigor" | **drop** | — | A label. |
| `research/ml/artifact.py:76` | same | **drop** | — | It states that QC date strings are converted. |
| `research/parity/qc_reconciler.py:25` | numerical-rigor taxonomy | **re-point** | E6 | E6 keeps the taxonomy "in lockstep with `DivergenceCategory`", and that enum is this file. |
| `research/parity/qc_reconciler.py:60` | same | **re-point** · codegen | E6 | Same. |
| `research/recency/runner.py:8` | numerical-rigor "no silent catches" | **drop** | — | A coding hard rule, cited from the wrong rule. The isolation behavior is stated. |
| `research/recency/service.py:119` | temporal-rigor (ET date) | **re-point** | ADR 0022 (a) | An ET-anchored trading date, never a UTC `strftime`. |
| `research/recency/stats.py:19` | AGENTS.md #5 | **re-point** | E5 | Merged list: every number is Python-authored. |
| `research/recency/stats.py:104` | CLAUDE.md #5 | **re-point** | E5 | A P&L formula mirroring the canonical `persisted_trade_net_pnl`. |
| `research/recency/stats.py:105` | same sentence | **re-point** | E5 | Same edit. |
| `research/return_distribution.py:23` | `math-sources-of-truth.md` | **drop** | — | As #2742. |
| `research/runs/result.py:4` | numerical-rigor "Timestamp rigor" | **drop** | — | A label. |
| `research/sweep/snapshot.py:43` | temporal-rigor ban list (one `mcal` importer) | **re-point** | ADR 0022 (d) | This is why the package version is read from metadata. |
| `research/sweep/snapshot.py:69` | temporal-rigor (ET-midnight anchor) | **re-point** | ADR 0022 (a) | Every date is a defined ET anchor. |
| `research/walk_forward/selection.py:67` | `math-sources-of-truth.md` | **drop** | — | As #2742. |
| `routers/broker_v2_gallery.py:106` | CLAUDE.md single-source-of-truth | **drop** | — | It states the reuse. The fill-authority branch is code structure. |
| `routers/data_lake.py:118` | temporal-rigor date anchor | **re-point** · codegen | ADR 0022 (a) | A trading date is a date-anchored ms instant. |
| `routers/data_lake.py:373` | temporal-rigor | **drop** | — | A section-header label. |
| `routers/data_lake.py:379` | temporal-rigor date anchor | **re-point** | ADR 0022 (a) | Anchored at the 09:30 ET open. |
| `routers/jobs.py:9` | "the project rule: Python owns all math, .NET is transport" | **re-point** | E5 | The math-authority rule itself. |
| `routers/lean_sidecar.py:117` | numerical-rigor "Timestamp rigor" | **drop** | — | A label. |
| `routers/lean_sidecar.py:274` | numerical-rigor (a `Field` description) | **drop** · codegen | — | A label, and the wrong rule. This is the live `/trusted-runs` request model. |
| `routers/lean_sidecar.py:948` | `.claude/CLAUDE.md` (no silent swallow) | **goes with dead code** | — | Inside `get_runs_index`; #2706 cuts `GET /runs`. |
| `routers/lean_sidecar.py:1203` | numerical-rigor | **goes with dead code** | — | The `RunReconciliationReportModel` of the reconcile route #2706 cuts. |
| `routers/lean_sidecar.py:1254` | numerical-rigor ($0.01) | **goes with dead code** · codegen | — | The reconcile route's docstring (#2706). `broker.types.ts:6184` goes with the regeneration. |
| `routers/lean_sidecar.py:1930` | numerical-rigor (Branch B) | **goes with dead code** | — | `_TradeRecordModel` serves only `POST /compare`, which #2706 cuts. |
| `routers/news.py:14` | temporal-rigor (deliberate deviation) | **re-point** | E7 | Merged list (#2742 I): `published_utc*` filter strings. |
| `routers/news.py:15` | CLAUDE.md "philosophy #4" | **re-point** | E7 | The same sentence; #4 is used for "state a deviation". |
| `routers/research_runs.py:38` | `python.md` (async by default) | **drop** | — | The docstring states why the threadpool path is right. |
| `routers/spec_strategy.py:73` | numerical-rigor "Timestamp rigor" | **drop** · codegen | — | A label, and the wrong rule. |
| `schemas/alpaca_clerk_sqlite.py:520` | AGENTS.md "Python owns all math" | **re-point** · codegen · money | E5 | Merged list. |
| `schemas/alpaca_live_verdict.py:38` | CLAUDE.md hard rule (operator copy) | **re-point** · money | ADR 0035 D12 | Merged table; ADR 0014 holds the same rule for broker narratives. |
| `schemas/backtest_runs.py:7` | `python.md` snake_case | **re-point** | ADR 0058 D6 | D6 records the deliberate camelCase exception (merged table). |
| `schemas/broker_bots.py:54` | CLAUDE.md #5 | **drop** | — | "Do not write a second regex" is stated. |
| `schemas/broker_configuration.py:17` | temporal-rigor admissible range | **re-point** | E7 | It is bounded by `MAX_TIMESTAMP_MS`, never `2**63 - 1`. |
| `schemas/broker_v2_evidence.py:6` | temporal-rigor | **drop** | — | A label. |
| `schemas/broker_v2_gallery.py:9` | temporal-rigor | **drop** | — | A label. |
| `schemas/broker_v2_gallery.py:64` | CLAUDE.md single-source-of-truth | **re-point** | E5 | "A frontend addition of two already-fetched numbers is still a second P&L": the math-authority rule. |
| `schemas/broker_v2_panel.py:8` | temporal-rigor | **drop** | — | A label. |
| `schemas/engine_availability.py:7` | "temporal rigor" | **re-point** | ADR 0022 (a) | Trading days anchor to their scheduled session open. |
| `schemas/engine_backtest.py:379` | numerical-rigor `QUANTITY_MISMATCH` | **re-point** | E6 | It classifies the persisted-P&L divergence with the taxonomy. |
| `schemas/fleet_history_batch.py:128` | temporal-rigor | **drop** | — | It states that no date crosses and the coordinator converts. |
| `schemas/grid_search.py:3` | temporal-rigor | **drop** | — | A label. |
| `schemas/news.py:9` | temporal-rigor | **drop** | — | It states the ingestion conversion and where it happens. |
| `schemas/news.py:18` | numerical-rigor (receipts) | **re-point** | E6 | Vendor sentiment cannot carry a fixture and tolerance, so it is recorded, never validated: E6's receipt rule. |
| `schemas/signal_program_seal.py:18` | CLAUDE.md #5 | **drop** | — | "Imports the alias rather than repeating the Literal" is stated. |
| `schemas/signal_program_seal.py:19` | same sentence | **drop** | — | Same edit. |
| `schemas/signal_program_seal.py:217` | CLAUDE.md "#2/#5" (Math Provenance Contract) | **re-point** · codegen | E5 | E5 makes the provenance block the record of canonical math. |
| `schemas/signal_program_seal.py:221` | `learn-ai-validation` skill | **re-point** · codegen | E5 | The same block; a skill is a rule file under the same ruling. |

### `PythonDataService/app/` — services, utils, volatility

| `file:line` | Rule cited | Verdict | Target | Reason |
|---|---|---|---|---|
| `services/account_pnl_reconciliation.py:30` | numerical-rigor accumulated-P&L default | **re-point** | E6 | `1e-6` is the rule's accumulated-P&L default, and it is returned to every consumer. |
| `services/account_pnl_reconciliation.py:70` | same (`Reference:` line) | **re-point** | E6 | Same. |
| `services/bar_persistence.py:26` | numerical-rigor "Ban list" (no silent repair) | **ADR owed** · feed | E7+a | A non-monotonic day is quarantined, not repaired. |
| `services/bot_runner.py:25` | temporal-rigor | **drop** · money | — | A label. |
| `services/broker_v2_panel/chart_projection_service.py:171` | CLAUDE.md #5 | **drop** | — | It states why the projection was promoted. It is not math. |
| `services/broker_v2_panel/gallery_hub.py:27` | CLAUDE.md single-source-of-truth | **drop** | — | It states the reuse. |
| `services/broker_v2_panel/panel_chart_data_source.py:115` | same | **drop** | — | It states the reuse ("never diverging on fill provenance"). |
| `services/broker_v2_panel/qualification_recorded_history.py:255` | temporal-rigor (session-literal ban) | **drop** | — | It explains why `time(0, 0)` is the vendor's midnight, not a session boundary. |
| `services/broker_v2_panel/qualification_recorded_history.py:270` | temporal-rigor (no hardcoded session) | **re-point** | ADR 0022 (b) | Every session comes from the calendar. |
| `services/dataset_service.py:43` | numerical-rigor "External-API ingestion" | **ADR owed** | E7+a | `CanonicalBarsError` surfaces duplicates and non-monotonic bars. |
| `services/dataset_service.py:278` | same | **ADR owed** | E7+a | The canonical-input path: no dedup and no re-sort. |
| `services/decision_clock.py:70` | CLAUDE.md #5 | **re-point** | ADR 0053 §15 | §15 records this exact duplicate, its canonical file and its parity test. |
| `services/engine_backtest_service.py:649` | numerical-rigor `DECISION_MISMATCH` | **re-point** | E6 | Taxonomy classification of the session-filter bug. |
| `services/iv_recorder.py:63` | "CLAUDE.md rule" (int64 ms) | **drop** | — | A label. |
| `services/jsonl_wal.py:48` | "CLAUDE.md guiding-philosophy #5" | **drop** | — | "Four copies of a security check are four places for a fix to miss" is the reason. It is not math. |
| `services/lean_sidecar_compare_service.py:48` | numerical-rigor taxonomy (`Reference:` line) | **re-point** | E6 | Live through `research/backtest_runs/parity.py`, even after #2706 cuts `POST /compare`. |
| `services/lean_sidecar_compare_service.py:113` | numerical-rigor tolerances | **re-point** | E6 | Same. |
| `services/options_companion_service.py:14` | `math-sources-of-truth.md` | **drop** | — | As #2742. "Parity pending" is debt; file it as an issue (merged list, For the map). |
| `services/polygon_client.py:341` | numerical-rigor (receipts) | **re-point** | E6 | Same as `schemas/news.py:18`. |
| `services/polygon_client.py:349` | temporal-rigor deviation; "CLAUDE.md philosophy #4" | **re-point** | E7 | Merged list (time is #6, not #4). |
| `services/polygon_notice_classifier.py:17` | CLAUDE.md #5 | **drop** | — | "Cannot drift onto a second vocabulary" is stated. It is copy, not math. |
| `services/quantlib_pricer.py:5` | `math-sources-of-truth.md` | **drop** | — | As #2742 (◇: the code stays). |
| `services/reference_companion_service.py:167` | temporal-rigor | **drop** | — | It states that the vendor string is not carried. |
| `services/sanitizer.py:4` | numerical-rigor "Two and only two conversion boundaries" | **ADR owed** | E7+a | This `Reference:` line names fail-fast duplicate detection. |
| `services/session_authority.py:54` | temporal-rigor (DST) | **drop** | — | It states that a fixed offset is an hour wrong. |
| `services/ta_service.py:82` | `numerical-rigor.md:62` (indicator `atol=1e-9`) | **re-point** | E6 | The warmup mask exists because of the indicator default. The line number is already fragile. |
| `utils/et_words.py:6` | temporal-rigor | **drop** | — | It states "display-only: never stored, parsed back, or compared". |
| `utils/session_anchors.py:4` | temporal-rigor + ADR 0022 | **drop** | — | ADR 0022 is already cited on the same line; only the rule-file half goes. |
| `utils/timestamps.py:3` | temporal-rigor | **re-point** · **hashed** | ADR 0022 (a) | This module's whole subject is the representation rule. |
| `volatility/solver.py:7` | `math-sources-of-truth.md` | **drop** | — | As #2742 (◇). |

### `PythonDataService/scripts/`

| `file:line` | Rule cited | Verdict | Target | Reason |
|---|---|---|---|---|
| `scripts/fixture_generators/return_distribution.py:14` | "per repo rules" (regeneration justified in the commit) | **re-point** | E6 | The fixture lifecycle rule. A comment-only edit; the emitted bytes must stay identical (H3). |
| `scripts/generate_signal_program_trace_corpus.py:9` | numerical-rigor (every fixture carries its regeneration command) | **re-point** | E6 | This is the script's reason to exist. Check #2743 hazard 8 first. |
| `scripts/measure_sweep_cell_footprint.py:70` | temporal-rigor (no hardcoded session times) | **re-point** | ADR 0022 (b) | The calendar owns the early closes. |

### `PythonDataService/tests/`

| `file:line` | Rule cited | Verdict | Target | Reason |
|---|---|---|---|---|
| `tests/_helpers/signal_program.py:182` | temporal-rigor | **re-point** | ADR 0022 (b, d) | Derived from the one calendar, never a session literal. |
| `tests/broker/alpaca/clerk/sqlite/conftest.py:140` | AGENTS.md "don't duplicate utility functions" | **drop** · money | — | A coding hard rule. |
| `tests/broker/alpaca/clerk/sqlite/test_schema_parity.py:1` | "Guiding-philosophy #5" | **re-point** · money | ADR 0035 annex | The DDL-vs-annex parity. |
| `tests/broker/alpaca/clerk/test_fifo_pnl.py:4` | numerical-rigor accumulated-P&L default | **re-point** · money | E6 | Golden parity (sacred): change the docstring only. **It says `1e-9` is the default; the rule says `1e-6`** (H7). |
| `tests/broker/alpaca/clerk/test_synthetic_activation.py:79` | temporal-rigor range | **re-point** · money | E7 | The domain ceiling, not the int64 width. |
| `tests/broker/alpaca/profile/test_runtime_context.py:241` | CLAUDE.md #5 | **drop** · money | — | It states what it pins. |
| `tests/broker/alpaca/profile/test_runtime_context.py:242` | same sentence | **drop** · money | — | Same edit. |
| `tests/broker/alpaca/test_config.py:207` | CLAUDE.md #5 | **drop** · money | — | It states that the parity test stops drift. |
| `tests/broker/alpaca/test_marketable_limit.py:127` | CLAUDE.md #5 | **drop** · money | — | `marketable_limit.py:22` states why. |
| `tests/broker/fleet/test_audit_routing_receipts.py:9` | temporal-rigor + numerical-rigor | **drop** · money | — | It introduces a list; each bullet states what is covered. |
| `tests/broker/fleet/test_audit_routing_receipts.py:10` | same sentence | **drop** · money | — | Same edit. |
| `tests/broker/fleet/test_audit_routing_receipts.py:806` | "temporal rigor" | **drop** · money | — | A section-header label. |
| `tests/broker/fleet/test_refusal_body_shape.py:562` | numerical-rigor "prove the check can fail" | **drop** · money | — | The rule has no such standard. The anti-vacuous purpose is stated. |
| `tests/broker/ibkr/test_minute_assembler.py:118` | temporal-rigor live relaxation | **re-point** · feed | ADR 0053 §14 | An exact redelivery is absorbed. |
| `tests/broker/ibkr/test_minute_assembler.py:129` | same | **re-point** · feed | ADR 0053 §14 | An older print of an emitted minute is fatal. |
| `tests/broker/v2panel/test_chart_projection.py:428` | temporal-rigor (bar = close) | **drop** | — | "Bars are labelled by their close" is stated. |
| `tests/broker/v2panel/test_qualification_recorded_history.py:74` | temporal-rigor | **drop** | — | It states the vendor midnight versus the hardcoded open. |
| `tests/broker_configuration/test_clerk_dir_parity.py:5` | CLAUDE.md #5 | **drop** | — | ADR 0060 D7 is already cited for the reason. |
| `tests/broker_configuration/test_legacy_environment.py:177` | CLAUDE.md #5 | **drop** | — | It states what it pins. |
| `tests/broker_configuration/test_legacy_environment.py:222` | same | **drop** | — | Same. |
| `tests/contracts/test_alpaca_configuration_source.py:17` | temporal-rigor (ban-list analogy) | **drop** | — | An analogy, not a reason. |
| `tests/contracts/test_fleet_role_openapi_agreement.py:109` | `testing.md` | **drop** | — | It states the reason: the test budget, with the daily run keeping the coverage. |
| `tests/contracts/test_handoff_script_worker_service.py:38` | CLAUDE.md #5 | **drop** | — | It states the reason: no second hand-kept expectation. |
| `tests/contracts/test_pre_commit_lint_gate_parity.py:5` | `.claude/CLAUDE.md` as "the documented CI gate" | **drop** | — | The CI workflow is the gate, and the test already names `ci.yml` (`:49`). The rules list (#2715) owns this test's subject. |
| `tests/contracts/test_pre_commit_lint_gate_parity.py:6` | `PythonDataService/CLAUDE.md` | **drop** | — | Same edit. |
| `tests/engine/indicators/test_vwap_reversion_indicators.py:6` | numerical-rigor tolerance | **goes with dead code** | — | SPY VWAP reversion is unused math that goes (◇ reading on the map; #2704). |
| `tests/engine/live/divergence/test_bar_series_joiner.py:6` | numerical-rigor | **goes with dead code** | — | #2704. |
| `tests/engine/live/test_qc_python_parity_fixture.py:62` | numerical-rigor | **goes with dead code** | — | The whole file is cut by #2727 (a parity test that never ran). |
| `tests/engine/strategy/algorithms/test_ema_crossover_signal_parameterized.py:4` | "the repo rule 'any tunable … is one'" | **drop** | — | No rule file holds it, and the docstring states the change. |
| `tests/engine/strategy/test_registry_signal_program_identity.py:202` | numerical-rigor | **drop** | — | It states the failure mode itself. |
| `tests/engine/strategy/test_signal_program_session_boundaries.py:12` | temporal-rigor | **re-point** | ADR 0022 (b) | The test's subject is boundaries derived from the calendar. |
| `tests/engine/strategy/test_signal_program_session_boundaries.py:185` | temporal-rigor | **drop** | — | It states why a DST date is discovered, not hardcoded. |
| `tests/engine/strategy/test_signal_program_session_boundaries.py:205` | temporal-rigor | **drop** | — | It states that the fixed offset is a contrast value only. |
| `tests/engine/strategy/test_signal_program_trace_corpus_generator.py:8` | numerical-rigor (regenerate-to-pass ban) | **re-point** | E6 | The fixture lifecycle rule. |
| `tests/engine/test_polygon_bars.py:5` | numerical-rigor | **drop** | — | A label. |
| `tests/fixtures/golden_support/compare.py:3` | numerical-rigor (explicit tolerances) | **re-point** | E6 | This is the comparator's reason to exist. A golden-support comment; not a hashed fixture file. |
| `tests/installation_migration/test_facts.py:49` | temporal-rigor range | **re-point** | E7 | The domain ceiling. |
| `tests/integration/data_lake/test_flag_flip_parity.py:8` | numerical-rigor equivalence levels | **re-point** | E6 | It states which level each claim targets. |
| `tests/integration/parity/test_ema_crossover_lean_vs_spec.py:6` | numerical-rigor gating set | **re-point** | E6 | The acceptance gate (☆ keeps this test). |
| `tests/integration/reconciliation/test_spy_vwap_reversion_qc.py:55` | numerical-rigor loosening rule | **goes with dead code** | — | SPY VWAP reversion goes (◇ reading; #2704). |
| `tests/integration/test_engine_persistence_quantity_pnl.py:7` | numerical-rigor `QUANTITY_MISMATCH` / `PNL_DRIFT` | **re-point** | E6 | Taxonomy classification. |
| `tests/lean_sidecar/test_cross_runner.py:172` | numerical-rigor | **drop** | — | A label; the assert says it. |
| `tests/lean_sidecar/test_reconciler.py:185` | numerical-rigor | **goes with dead code** | — | Tests `lean_sidecar/reconciler.py` (#2704). |
| `tests/research/parity/test_ibkr_commission_golden.py:8` | numerical-rigor bit-exact level | **re-point** | E6 | The equivalence level. Golden parity: change the docstring only. |
| `tests/research/recency/test_stats.py:1` | AGENTS.md #5 | **re-point** | E5 | Same as `recency/stats.py:19`. |
| `tests/research/recency/test_stats.py:7` | `math-sources-of-truth.md` | **drop** | — | As #2743. |
| `tests/research/recency/test_stats.py:83` | CLAUDE.md #5 | **re-point** | E5 | Same as `recency/stats.py:104`. |
| `tests/routers/test_chart_range_presets.py:150` | "AGENTS.md hard rule on ISO-free wire" | **drop** | — | A label. |
| `tests/routers/test_data_lake_backfill_job.py:368` | temporal-rigor | **re-point** | ADR 0022 (a) | The ET-session-open anchor. |
| `tests/routers/test_engine_bars_endpoint.py:75` | temporal-rigor bar alignment | **goes with dead code** | — | #2706 cuts `GET /api/engine/bars`, and all 6 tests drive it. |
| `tests/routers/test_news_endpoint.py:64` | `testing.md` | **drop** | — | It states why the canary is deliberate. |
| `tests/routers/test_news_endpoint.py:101` | temporal-rigor | **drop** | — | It states that the vendor string is not kept. |
| `tests/routers/test_recency_chart_job.py:25` | temporal-rigor | **re-point** | ADR 0022 (a) | An ET-anchored trading date. |
| `tests/services/bot_runner/_support.py:111` | temporal-rigor | **drop** | — | A label. |
| `tests/services/test_bar_persistence.py:13` | numerical-rigor "Timestamp rigor" | **ADR owed** · feed | E7+a | Never silently repair. |
| `tests/services/test_bar_persistence.py:120` | same | **ADR owed** · feed | E7+a | Quarantine, not repair. |
| `tests/services/test_candidate_uncaptured_at_crash.py:15` | CLAUDE.md "don't duplicate utility functions" | **drop** · money | — | A coding hard rule. |
| `tests/services/test_chart_range_presets.py:56` | "AGENTS.md hard rule" | **drop** | — | A label. |
| `tests/services/test_gallery_hub.py:535` | CLAUDE.md single-source-of-truth | **drop** | — | It states what it pins. |
| `tests/services/test_lean_sidecar_template_registry.py:89` | CLAUDE.md "guiding philosophy #2" (receipts) | **re-point** | E6 | A same-trades claim is numerical and needs a receipt. |
| `tests/services/test_reference_companion_news_csv.py:57` | temporal-rigor | **drop** | — | It states it. |
| `tests/services/test_signal_program_crash_replay.py:29` | `python.md` (seeded RNG) | **drop** | — | It states reproducibility. |
| `tests/test_indicators_endpoint.py:21` | numerical-rigor | **goes with dead code** | — | The whole file is cut by #2731 (row 20, duplicate). |
| `tests/test_regime.py:15` | numerical-rigor timestamp policy | **drop** | — | A label. #2731 flags the module behind it as possibly dead. |
| `tests/test_sanitizer.py:44` | numerical-rigor fail-fast | **ADR owed** | E7+a | Duplicates raise. |
| `tests/test_statistics.py:594` | temporal-rigor | **drop** | — | It states the reason. |
| `tests/unit/data_lake/test_cache_import.py:379` | temporal-rigor | **goes with dead code** | — | #2705. |
| `tests/unit/data_lake/test_sessions.py:6` | "CLAUDE.md guiding-philosophy #5" + temporal-rigor "Calendar authority" | **re-point** | ADR 0022 (d) | The adapter's parity test against the one calendar. |
| `tests/unit/data_lake/test_sessions.py:7` | same sentence | **re-point** | ADR 0022 (d) | Same edit. |
| `tests/unit/data_lake/test_sessions.py:70` | "guiding-philosophy #5" (duplicate needs parity) | **re-point** | ADR 0022 (d) | Same. |

### `Backend/`

| `file:line` | Rule cited | Verdict | Target | Reason |
|---|---|---|---|---|
| `Backend/Services/Implementation/PositionEngine.cs:26` | `math-sources-of-truth.md` (F-0010) | **re-point** | E5 | As #2743 (its ADR owed A merged into entry 5). E5 names .NET FIFO lots as the one exception. |

### `Frontend/src/app/`

| `file:line` | Rule cited | Verdict | Target | Reason |
|---|---|---|---|---|
| `api/broker-models.ts:5` | `Frontend/AGENTS.md` | **drop** | — | That file does not exist. The command is `npm run codegen:openapi`. |
| `components/broker/broker-deploy-page/alpaca-deploy-workflow.symbol-scope.spec.ts:365` | temporal-rigor | **re-point** · money | ADR 0022 (e) | Rendered by the shared display component, never a client clock. |
| `components/broker/shared/extended-flatten-ticket/extended-flatten-ticket.component.ts:69` | AGENTS.md "Python owns all math" | **re-point** · money | E5 | The component derives no execution or cost figure. |
| `components/broker/v2-panel/cohort-flatten/cohort-flatten-confirmation.ts:6` | CLAUDE.md closed operator-copy-map rule | **drop** · money | — | The comment records the owner decision (2026-09-23, #1909) and names the spec that pins it. |
| `components/broker/v2-panel/gallery/lib/candle-renderer.ts:77` | CLAUDE.md #5 | **goes with dead code** | — | The `showLastPriceTag` key and branch are cut by #2708 row 5. |
| `components/broker/v2-panel/gallery/lib/candle-renderer.ts:389` | temporal-rigor | **goes with dead code** | — | The docstring of `drawLastPriceTag`, cut by #2708 row 5. |
| `components/broker/v2-panel/gallery/lib/gallery.types.ts:12` | "the single-canonical-implementation rule" | **drop** | — | It states the reuse. |
| `components/broker/v2-panel/gallery/lib/gallery.types.ts:14` | temporal-rigor | **drop** | — | A label. |
| `components/broker/v2-panel/lib/broker-v2-panel.types.ts:4` | temporal-rigor | **drop** | — | A label. |
| `components/broker/v2-panel/lib/panel-action-outcome.spec.ts:244` | CLAUDE.md (backend-authored copy) | **re-point** · money | ADR 0035 D12 | Server prose renders verbatim. |
| `components/edge/realized-vs-iv/realized-vs-iv.component.spec.ts:18` | `angular.md` "Testing" | **drop** | — | A test-style convention. |
| `fleet/clerk-scoped-url.ts:16` | CLAUDE.md #5 | **drop** | — | It states the reuse. |
| `services/lean-sidecar.types.ts:9` | numerical-rigor | **drop** | — | A label. The int64-versus-2^53 note stays. |
| `shared/data-lake/data-lake.service.spec.ts:38` | temporal-rigor | **re-point** | ADR 0022 (a) | A trading-date parameter is ms, never an ISO date. |
| `shared/data-lake/data-lake.types.ts:12` | temporal-rigor | **re-point** | ADR 0022 (a, e) | Anchored at the session open and rendered `date-et`. |
| `shared/data-lake/data-lake.types.ts:159` | temporal-rigor (its own "documented exception") | **ADR owed** | E7+b | The backfill body anchors at 12:00 UTC, not an ET session instant: a deviation from ADR 0022 (a) that entry 7 does not list. |
| `shared/data-lake/trading-range.ts:64` | temporal-rigor | **ADR owed** | E7+b | `tradingDateToMs` computes that noon-UTC anchor, and its docstring holds the reasoning. |
| `shared/date/et-midnight.ts:5` | temporal-rigor "Date-anchored values" | **re-point** | ADR 0022 (a) | An ET-anchored boundary. |
| `shared/date/local-wall-clock.ts:7` | temporal-rigor (`new Date(string)` ban) | **drop** | — | It states the method: numeric parts, never parsing. |
| `shared/errors/error-catalog.ts:14` | `math-sources-of-truth.md` | **drop** | — | As #2743. No producer sets `mathRef` (For the map, 5). |
| `shared/indicators/indicator-reference.ts:4` | "CLAUDE.md rule 5: Python owns all math" | **re-point** | E5 | The layering note is the math-authority rule. |
| `shared/ticker-catalog/ticker-catalog.service.ts:152` | temporal-rigor | **re-point** | ADR 0022 (a, e) | ET-anchored dates must not drift a day. |
| `utils/black-scholes.ts:8` | AGENTS.md "Python owns all math" | **re-point** | E5 | The same sentence as #2743's `:7` row (owed A, now entry 5). One edit. |

## Counts

| Verdict | Rows |
|---|---|
| drop | 114 |
| re-point → ADR in force | 46 (0022 ×30, 0035 ×11, 0053 ×4, 0058 ×1) |
| re-point → merged entry | 48 (E5 ×15, E6 ×25, E7 ×8) |
| ADR owed beyond the merged list | 14 (E7+a ×12, E7+b ×2) |
| goes with dead code | 24 |
| **Total** | **246** |

## ADRs owed beyond the merged list

Both widen **entry 7 (amend ADR 0022)** rather than add a new ADR, because both are the time rule's own decisions.

- **E7+a — Finite ingestion fails fast; it never repairs.**
  - Decision:
    - A finite vendor fetch (a historical bar window, a cache file, a Polygon chunk set) is validated where it is ingested.
    - A duplicate or non-monotonic timestamp is refused, or the day is quarantined, with a descriptive error.
    - There is no `drop_duplicates`, forward-fill or reorder.
    - Live subscriptions are the narrow exception that ADR 0053 §14 already records.
  - Why: a duplicate or gap in a closed dataset is upstream corruption, and repairing it hides that.
  - No ADR in force records it. I grepped every ADR for `fail fast`, `drop_duplicates`, `non-monotonic` and `forward-fill`. ADR 0053 has the live half (`:105-109`) and the feed-continuity "nothing is interpolated, forward-filled or reordered" (`:47`). ADR 0022 records representation but not the ingestion check.
  - The rule lives only in `.claude/rules/temporal-rigor.md` "Two and only two conversion boundaries" and "Finite ingestion vs. live subscriptions".
  - 12 rows lean on it: `minute_assembler.py:26`, `bar_validation.py:4`, `polygon_canonical.py:206/255/327`, `bar_persistence.py:26`, `dataset_service.py:43/278`, `sanitizer.py:4`, `test_bar_persistence.py:13/120` and `test_sanitizer.py:44`. The dead `cache_import.py` rows lean on it too.
- **E7+b — The data-lake noon-UTC trading-date anchor.**
  - Decision: the backfill, ensure-data and coverage request windows carry a trading date as 12:00:00.000 UTC of the calendar date, not the ET session open.
  - Why: the value is computed by pure UTC arithmetic, so it never depends on the browser's zone or a DST boundary. It stays unambiguous inside the ET day (#1877).
  - It departs from ADR 0022 (a) ("a defined ET session anchor") and is missing from entry 7's list of accepted deviations.
  - It lives in `trading-range.ts:61-80`, `data-lake.types.ts:155-165` and `app/data_lake/types.py::trading_date_to_calendar_anchor_ms`.

## Not comments (listed, not judged)

- **Generated types (codegen).** `Frontend/src/app/api/broker.types.ts` is never hand-edited. Each line follows its Python source through `export_openapi_contract.py` and `npm run codegen:openapi`:

  | `broker.types.ts` line | Python source row |
  |---|---|
  | `:4798` | `routers/data_lake.py:118` |
  | `:6184` | `routers/lean_sidecar.py:1254` (dead) |
  | `:10751` | `broker/contract/models.py:353` |
  | `:14797` | `research/parity/qc_reconciler.py:60` |
  | `:20564` | `schemas/signal_program_seal.py:217` |
  | `:20568` | `schemas/signal_program_seal.py:221` |
  | `:22209` | `schemas/alpaca_clerk_sqlite.py:520` |
  | `:25559` | `routers/spec_strategy.py:73` |
  | `:27660` | `routers/lean_sidecar.py:274` |

- **Sealed fixture text.** These are emitted attribution strings and manifest data. Never hand-edit them; regenerate with a justified commit or leave them:
  - `PythonDataService/scripts/fixture_generators/return_distribution.py:279`: the RD-001 attribution, which the manifest hashes (`test_attribution_hashes_match_disk`).
  - `PythonDataService/scripts/fixture_generators/strategy_abc_self_equivalence.py:246`.
  - `scripts/fixture_generators/ibkr_iv.py:136`: OPT-IB-002 (◇), emitted into `options-pricing/OPT-IB-002/v1/attribution.md:23`.
  - `PythonDataService/tests/fixtures/golden/manifest.json:1730`: a tolerance note.
  - 15 golden `attribution.md` and `README.md` files under `tests/fixtures/` that cite rule files. They were excluded from the census as `*.md`; see Not reviewed.
- **Error and assertion text.**
  - `app/data_lake/cache_import.py:595` is error text; it goes with dead code.
  - `tests/contracts/test_pre_commit_lint_gate_parity.py:38` and `:49` are failure messages. Reword them with the `:5-6` drop.
- **Frontend test data.**
  - `shared/errors/error-catalog.spec.ts:48` and `:51` use `'/docs/math-sources-of-truth.md'` as a fake `mathRef` value. The value is arbitrary, so the test stays valid when the doc is cut; swapping the literal is optional.
  - `page-error.component.spec.ts:18` is a test name; it goes with dead code (#2709 A21).
- **Tests and checkers that read rule files and assert on their text.** I point at these and give them no rows. The rules list (#2715), the gates list (#2716) and the contract-tests list (#2730) own them:
  - `scripts/check_documentation_contract.py` (`:19-20`, `:37` protected-canonical `math-sources-of-truth.md`, `:180-255` AGENTS.md checks)
  - `PythonDataService/tests/contracts/test_documentation_contract.py:28-79`
  - `PythonDataService/tests/contracts/test_pytest_configuration.py:21-23`, which reads `.claude/CLAUDE.md` and `PythonDataService/CLAUDE.md` for the fast-test command

## Hazards the cutting PR must carry

- **H1 Hashed files.** `engine/strategy/normalized_gap.py:4` and `utils/timestamps.py:3` are Signal Program build-proof sources. Editing either changes every program's source digest. They ride the **one** re-qualification PR, with #2742 H1's rows and #2704's hashed edits.
- **H2 Codegen.** The 9 codegen rows change the OpenAPI snapshot and `broker.types.ts`.
  - Edit the Python text first, then export and run codegen.
  - Those PRs merge serially.
  - The dead `lean_sidecar.py:1254` description disappears with #2706's route cut and needs no edit.
- **H3 Sealed fixture text.**
  - The comment-only edit at `return_distribution.py:14` must leave the emitted bytes identical. Regenerate and diff to prove it.
  - Never hand-edit the attribution strings or the manifest note listed above.
- **H4 Money path.** The rows marked "money" sit in clerk, Alpaca, fleet and flatten files. Edit the comment or docstring only, then re-run the clerk SQLite, Alpaca, fleet and v2-panel suites. Do not reflow code on the way through.
- **H5 Sacred IBKR feed.** The rows marked "feed" sit in `ibkr/client.py`, `minute_assembler.py`, `bar_models.py`, `capability.py`, `bar_persistence.py` and their tests. Edit docstrings and comments only, then re-run the IBKR feed and market-data suites.
- **H6 Rows wait for their ADR.**
  - Drops and re-points to ADRs in force (0022, 0035, 0053, 0058) can land at once.
  - E5, E6, E7, E7+a and E7+b rows keep their rule citation until that ADR is accepted (#2742 H5), because the citation is the only written reason today.
  - So the rule rewrite (#2715) must not delete the cited `numerical-rigor.md` / `temporal-rigor.md` sections before E6/E7 land.
- **H7 Entry 6 must carry the per-kind tolerance table.**
  - Six live rows cite a specific default: indicator `1e-9` (`ta_service.py:82`), probability `1e-10` (`edge_score.py:87`), accumulated P&L (`account_pnl_reconciliation.py:30/70`, `test_fifo_pnl.py:4`) and fill price $0.01 (`order_evidence.py:103`). The commission $0.01 rows go with dead code.
  - E6 as written names only the strict-float default, so it must record the table or those rows lose their number's reason.
  - **Conflict:** the rule's accumulated-P&L default is `atol=1e-6`, and `account_pnl_reconciliation.py` uses that. `test_fifo_pnl.py:4` and its manifest note (`manifest.json:1730`) use `1e-9` and call it "the accumulated-P&L default". E6 should say that `1e-6` is the default and `1e-9` is the FIFO fixture's tighter pin. The sealed note itself stays.
- **H8 The SQLite DDL stays untouched.** The edits at `schema.py:10` (a module docstring) and `test_schema_parity.py:1` are outside `SCHEMA_DDL` and its SQL comments (#2742 H4).
- **H9 CLAUDE.md numbering.** After these rows land, no code comment cites a philosophy or rule number. Of the 37 rows that cite one, 22 drop, 14 re-point and 1 goes with dead code; a few "same sentence" continuation rows ride the same edits. #2715 is then free to renumber.
- **H10 Kill lists age.** Re-run the census at the cutting SHA, *with* the widened patterns above (`philosophy #`, `guiding-philosophy`, `temporal rigor`, `repo rule`, `project rule`, `single-canonical`, skill names).

## Pointers (outside this area)

- **Frontend dead code (#2709).** No producer anywhere sets `extensions.mathRef`: no Backend, no Python. `resolveMathRef` therefore always returns `undefined`. The `@if (info.mathRef …)` branches in `shared/errors/section-error.component.ts:40` (and in the already-dead `page-error`) never render. That is a dead branch the Frontend list missed. The `mathRef` field and its spec cases go with it.
- **ADR text.** `docs/architecture/adrs/0053-feed-continuity-same-run-recovery.md:113` (§15) and `0022:22,30` cite CLAUDE.md #5 and the rule files. The ADR handoffs should reword them when E5 and E7 land, or the ADRs keep citing rule files.
- **Scripts (#2707).** `PythonDataService/scripts/measure_sweep_cell_footprint.py:72` builds `datetime(…, 9, 30, tzinfo=EASTERN)` while its comment at `:70` credits the calendar. This is a minor literal in a measurement script.
- **Vendored reference.** `references/quantconnect/spy_vwap_reversion/normalize_orders.py` cites a rule file. It is outside this area and goes with the SPY VWAP reversion cut.

## Not reviewed

- **Golden attribution and README files** that cite rule files (15 under `PythonDataService/tests/fixtures/`, plus `references/README.md`). The census excluded `*.md`, and these are sacred paperwork. Several have attributions hashed by the manifest (ENG-008, FQ-001, RD-001). The math-notes ticket (#2714) or a fixture pass should judge them, checking each one per fixture.
- **Markdown under `app/`** and other non-code files.
- **Liveness, taken from the dead-code lists without re-tracing callers:**
  - the edge modules (`regime_clustering.py`, `edge_score.py`, `trade_simulator.py`)
  - `sanitizer.py`, after the `/api/sanitize` cut
  - `account_pnl_reconciliation.py`, after the P&L-attribution router cut; it still has a `brokers.py` importer
  - the module behind `tests/test_regime.py`

  Each row there flips to "goes with dead code" if its cut lands. The verdict is drop or re-point either way.
- **ADR clauses** beyond those named under "How this was judged". ADR 0014 was checked only by its heading and Decision paragraph.
- **The ~35 plain "single source of truth" prose lines** that name no rule. They are not citations, so I gave them no rows.

## For the map

1. **Rule-level question: does entry 5 cover non-math duplication?**
   - E5 is scoped to math ("one canonical implementation per math concept").
   - 28 rows use CLAUDE.md #5, "single source of truth" or "don't duplicate" for non-math reuse: a URL prefix, a symbol regex, a payload-hash rule, a fill-provenance branch, a mode table. Under the math-only reading they **drop**, and the counts above assume that.
   - Options:
     - **(Recommended)** Math only. Non-math "don't duplicate" stays a coding rule in CLAUDE.md, and those comments already state their own reason.
     - Widen E5 to every concept. Those 28 drops become re-points to E5.
2. **Widen entry 7** with E7+a (finite ingestion fails fast, 12 rows, money and feed files among them) and E7+b (the data-lake noon-UTC anchor, 2 rows). Without them, those rows have no ADR to point to once `temporal-rigor.md` is slimmed.
3. **Entry 6 needs the per-kind tolerance table and the P&L default conflict settled** (H7).
4. **About 28 comments cite `numerical-rigor.md` for time**: 17 for timestamp labels and 11 for fail-fast ingestion. ADR 0022 moved both sections out in 2026-07. Four cite sections that never existed or no longer exist: "External-API ingestion", "hash-chained-schema rule", "Decimal discipline" and "prove the check can fail". All are handled above. Nothing is lost by dropping them.
5. **A dead branch the Frontend list missed:** the `mathRef` error link (Pointers).
