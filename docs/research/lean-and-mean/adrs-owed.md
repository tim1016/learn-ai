# ADRs owed — one merged list (#2745)

Part of map #2700. This is a plan only. No ADR text is written here, and nothing is edited or deleted.

- **Read at:** `origin/master` **`6a4d7d396108ef16471d8df888b9ded74d3c2892`**. Every ADR `file:line` below is at that SHA. The four input lists were read at the same SHA.
- **Inputs:**
  - [Comment citations — Python app code (#2742)](https://github.com/tim1016/learn-ai/blob/research/lean-comment-citations-python/docs/research/lean-and-mean/comment-citations-python.md): ADRs A–I.
  - [Comment citations — scripts, Backend, Frontend and tests (#2743)](https://github.com/tim1016/learn-ai/blob/research/lean-comment-citations-rest/docs/research/lean-and-mean/comment-citations-rest.md): ADRs A–D.
  - [Second docs pass — kept architecture, authority and reference docs (#2741)](https://github.com/tim1016/learn-ai/blob/research/lean-docs-second-pass-standing/docs/research/lean-and-mean/docs-second-pass-standing.md): ten kept whys.
  - [Second docs pass — kept plans, PRDs, audits and research notes (#2740)](https://github.com/tim1016/learn-ai/blob/research/lean-docs-second-pass-one-off/docs/research/lean-and-mean/docs-second-pass-one-off.md): two.
  - [Kill list — ADRs (#2712)](https://github.com/tim1016/learn-ai/blob/research/lean-docs-adrs/docs/research/lean-and-mean/docs-adrs.md), for which ADRs are in force.
  - Two owner rulings relayed by the coordinator on 2026-09-30: (1) comments cite ADRs only, never rule files, so each standing rule those comments lean on needs an ADR in force; (2) `docs/math-sources-of-truth.md` is cut, so the math-authority ADR becomes the home of the duplicate rule.

## How this was judged

- **Owed** only for a strong reason: money-path safety, a numerical choice, a vendor constraint, or a boundary. Entries that only say where code lives, or that restate a comment that already carries its why, were dropped.
- **"Amend" targets** were checked on `origin/master`: each has `Status: Accepted` and none is on #2712's cut list (0003, 0005, 0006, 0007, 0009, 0010, 0013, 0016, 0017, 0019, 0024, 0025, 0028, 0041).
- **"New" entries** were checked by grepping every ADR in force for the decision's key terms (for example `epsilon`, `extended hours`, `sidecar`, `iv_recorder`, `params_hash`, `strict float`, `stream_epoch`). Where an ADR in force already records part of an entry, that part became a re-point and the entry shrank to what is still missing.
- **Rule-file citations.** For each standing rule that comments cite (`CLAUDE.md`, `AGENTS.md`, `.claude/rules/*`), I checked whether an ADR in force records it. Entries that those comments lean on are marked **rule-file citations lean on this**. The full map is under [Rule-file citations](#rule-file-citations-where-each-one-points).
- **Priority.** Money path first, then security boundary, then the rule backbone (rules many comments cite), then research and numerics. Inside a tier, an ADR that blocks a planned doc cut goes first.

## The list, in priority order

| # | New or amend | Title | Tier |
|---|---|---|---|
| 1 | Amend **ADR 0059** | Alpaca venue constraints on the money path | money path |
| 2 | Amend **ADR 0036** | Custody numeric rules: quantity, price and money | money path |
| 3 | Amend **ADR 0035** | Clerk store on VM-local storage; live snapshot stream semantics | money path |
| 4 | **New** | LEAN sidecar is a pinned, separately launched reference engine | security boundary |
| 5 | **New** | Python owns canonical math; other copies are parity-tested mirrors | rule backbone |
| 6 | **New** | Numerical rigor: strict-float equivalence, receipts and accepted departures | rule backbone |
| 7 | Amend **ADR 0022** | Accepted deviations from the int64-ms wire rule; the admissible range | rule backbone |
| 8 | **New** | Volatility ownership and IV recording | numerics / boundary |
| 9 | **New** | Research run identity and sealed run inputs | research reproducibility |
| 10 | **New** | Research verdict rules | numerical choice |
| 11 | Amend **ADR 0043** | Per-run build evidence is dynamic, file-backed run evidence | seal boundary |

Five are amendments and six are new. Two ADR 0035 amendments share one row.

---

### 1. Amend ADR 0059 — Alpaca venue constraints on the money path

- **Target:** ADR 0059 D1 (cash bound, `0059:88`) and D5 (extended hours, `0059:99-107`). Accepted, not cut.
- **Decision:**
  - D1 says a recorded fill stays reserved "until the next broker observation that can see the fill". The amendment defines "can see": a fill recorded less than 5 s before an observation's read stamp is still reserved. Alpaca documents no ordering between account cash and trade updates. The 5 s margin was confirmed by measurement (#2487), and the worst bound measured was 401 ms.
  - An operator flatten outside the regular session is an extended-hours DAY limit that the operator prices from the Clerk's live quote, inside a bps allowance. The Clerk refuses a stale or missing quote and a price the venue would reject. The confirmed price covers exactly the reviewed quantity and expires with its session, and with no session open the flatten is refused (owner decisions of 2026-09-19 on #2007).
- **Where the why lives today:** `PythonDataService/app/broker/alpaca/clerk/live_envelope.py:68-102` (the margin, its vendor reasoning and the measurement), `docs/references/alpaca-live-envelope.md` (the Alpaca pages cited), and the fixture `tests/fixtures/alpaca/fill_visibility/paper-btcusd-2026-09-28.json`. Also `PythonDataService/app/broker/alpaca/clerk/recovery_reduction.py:1-41` and `docs/references/alpaca-extended-hours.md:107-136`. ADR 0045 already knows of the priced flatten: `0045:74` says "confirmed safe-flatten limit is never replaced automatically".
- **What waits on it:** the comment rows `live_envelope.py:77` and `recovery_reduction.py:38` (#2742 A). No doc cut is blocked. Both notes stay for their vendor facts (#2714), and the extended-hours note's "Operator flatten" section can slim to vendor facts once this lands.
- **Merged from:** #2742 A, which already suggested ADR 0059 as the home.

### 2. Amend ADR 0036 — Custody numeric rules: quantity, price and money

- **Target:** ADR 0036 (Accepted, not cut). Its D1 is the one flatness rule, `POSITION_QTY_EPSILON = 1e-9` (`0036:20-24`), and its D2 is "the Frontend holds no flatness boundary". The amendment widens it from the flatness rule to every custody numeric boundary.
- **Decision:**
  - Fill-quantity equality uses an absolute `1e-9`, the same as flatness (`FILL_QTY_EPSILON`). It absorbs float64 aggregation residue without treating a real fractional-share remainder as complete.
  - A cumulative broker fill is priced on its delta: `delta_price = (cum_qty × cum_avg − prior_qty × prior_avg) / delta_qty`. Alpaca's `filled_avg_price` is a whole-order average.
  - At the same quantity, an average-price difference under $0.01/share is vendor rounding, because Alpaca publishes cents. A difference of $0.01/share or more is an `EXECUTION_PRICE_CONFLICT` (#2460).
  - Budget money is exact `Decimal` arithmetic. Legacy floats enter through `Decimal(str(x))`, inexact arithmetic traps, consent is whole cents and never rounds, required cents round up and spendable cents round down, and affordability compares with no epsilon (PRD #2540, a closed issue).
- **Where the why lives today:** `docs/references/clerk-invariants.md:46-232` (§2), `PythonDataService/app/broker/alpaca/clerk/sqlite/execution_coverage.py:46-48`, `manual_order_completion.py:35-38`, `order_evidence.py:96-104` and `folds.py:1325-1340`. For money: `docs/references/custody-budget-money.md:1-25` and `PythonDataService/app/broker/alpaca/clerk/money.py:1-13`, whose `Reference:` is the PRD #2540 issue link. ADR 0059's budget amendment (`0059:260-262`) names only the "whole-cent dollar commitment".
- **What waits on it:**
  - Comment rows `sqlite/execution_coverage.py:47`, `sqlite/manual_order_completion.py:37`, `sqlite/order_evidence.py:103` and `sqlite/folds.py:1334` (#2742 B), plus `money.py:4`. That last citation is an issue link, not a `.md` path, so the census did not catch it.
  - Doc cuts: `custody-budget-money.md` (kept by #2741 only as owed), and the decision parts of `clerk-invariants.md` §1–§3, which can then slim to validation receipts.
- **Re-point now, no ADR needed:**
  - `folds.py:946` and `:958` (flatness) re-point to ADR 0036 D1.
  - `facts.py:487` (an EXIT's quantity is the proven remaining attributed quantity) re-points to ADR 0030, which says EXIT "reduces the final instance-attributed quantity exactly" (`0030:102`).
  - `execution_coverage.py:47` also re-points to ADR 0035's `EXACT_REPLACES_CUMULATIVE` rule (`0035:260`), which says "within the pinned execution tolerance" but not the value.
- **Merged from:** #2742 B and #2741 kept why 7 (custody money normalization). Both are custody numerical choices on the money path with no ADR value.

### 3. Amend ADR 0035 — Clerk store on VM-local storage; live snapshot stream semantics

- **Target:** ADR 0035 (Accepted 2026-08-10, not cut). The two amendments go in one pass.
- **Decision (a), storage:** a clerk SQLite store lives on VM-local persistent storage, never on a host bind mount. WAL needs every connection in one locking and shared-memory domain, and the macOS Podman bind mount is virtiofs, which is not. That mount corrupted the clerk DB in the 2026-08-07 soak. A startup guard refuses a WAL store on such a filesystem.
- **Decision (b), live snapshots, under D12:** D12 already moves the live roster onto `SurfaceHub` + `versioned-snapshot-stream` (`0035:188-193`). The amendment records the semantics that came from the retired ADR 0028:
  - One producer owns each versioned snapshot.
  - A new `stream_epoch` replaces the snapshot, and inside one epoch only a higher `surface_version` advances it.
  - Client queues hold one slot, latest-wins.
  - The why: an operator surface must show the latest truth after a producer restart, never a merged or replayed backlog. Full snapshots were chosen over JSON Patch.
- **Where the why lives today:**
  - (a) `compose.yaml:127-132`; the guard at `PythonDataService/app/broker/alpaca/clerk/sqlite/repository_lifecycle.py:145-160`; the incident in `docs/audits/alpaca-sqlite-clerk-paper-soak-2026-08-07.md:312-353`.
  - (b) `docs/architecture/adrs/0028-bot-cockpit-operator-plane-authority-and-channel-contracts.md:90-156`, and `:373-394` for the alternatives; `Frontend/src/app/services/versioned-snapshot-stream.ts:25-31`.
- **What waits on it:**
  - Doc cuts: the soak report (#2740 cuts it) and **ADR 0028 itself** (#2712 cuts it). #2743 hazard 6 says to land this or reword the citing lines in the same PR.
  - Comment rows `Frontend/src/app/services/versioned-snapshot-stream.ts:25` and `PythonDataService/tests/services/test_surface_hub.py:1` (#2743 D).
- **Not owed (re-point):** #2741 kept why 1 asked for ADR 0035 to absorb the pinned contract's §4 transaction matrix and §6 operation-first custody. ADR 0035 already makes that document its binding annex: "The concrete schema DDL, PRAGMA set, transaction matrix …" (`0035:333-340`). Citations of §3a–§6 stay valid as ADR 0035 citations, and the decisions need no move.
- **Merged from:** #2740 ADR owed 1, #2743 D (as an ADR 0035 D12 amendment rather than a new ADR, because D12 already adopted the infrastructure), and #2741 kept why 1 (resolved as a re-point).

### 4. New — The LEAN sidecar is a pinned, separately launched reference engine

- **Check:** no ADR in force records it. ADRs 0022 (`:12`), 0049 (`:41`, `:53`, `:98`), 0058 (`:13`) and 0063 (`:176`) only mention the sidecar.
- **Decision:**
  - A separate launcher process alone owns Podman, so the data plane cannot escalate through its FastAPI handlers, and there is no Podman inside the data plane.
  - The LEAN image is pinned by digest in `config.py`.
  - Every run has mandatory limits: wall-clock timeout and per-request input ceilings. Every container flag maps to a stated boundary.
  - Only the trusted sample algorithms run, never caller-supplied source.
  - LEAN data-folder fidelity and the corporate-action policy hold.
  - The brokerage, fill, fee, fill-forward, normalization, date-window and quantization policies, and the statistics parity scope, are fixed.
  - From the mission-critical owner decisions:
    - D2: the determinism gate may differ only in `EndTime`, `Hostname` and `StartTime`, because those record the wall-clock run and container, not backtest semantics.
    - D3: the reconciler's scope.
    - D5: the quote and factor/map data source.
    - D10: the reconciler output schema.
- **Where the why lives today:**
  - `docs/architecture/lean-sidecar-lab.md`: authority boundary `:56-67`, container boundary and timeout `:93-134`, data fidelity and corporate actions `:164-206`, launcher topology `:207-226`, runner pin and compatibility `:294-331`, and the execution policies `:351-448`.
  - `docs/architecture/lean-sidecar-mission-critical.md`: D2 `:54-76`, D3 `:77-106`, D5 `:127-155`, D10 `:217-238`.
- **What waits on it:**
  - Comment rows `app/lean_sidecar/__init__.py:3`, `config.py:7`, `:51`, `:177`, `:242`, `launcher/__init__.py:4`, `launcher_client.py:5`, `runner.py:6`, `staging.py:363` and `app/services/lean_sidecar_service.py:13` (#2742 C).
  - Test docstrings `tests/lean_sidecar/test_security_flags.py:3` (#2743 C; it says "the results land in the ADR") and `tests/lean_sidecar/test_determinism_gate.py:76` (#2741).
  - Doc cuts: **`lean-sidecar-mission-critical.md` cannot be cut until D2/D3/D5/D10 move here** (#2741 hazard 8). After this lands, `lean-sidecar-lab.md` keeps only operator procedure, which moves to a runbook. `scripts/lean_sidecar_pin_image.py` step 2, which copies the digest into the doc, goes in the same pass (#2743 hazard 7).
- **Rows not taken (resolving a conflict between inputs):** #2742 put `workspace.py:13`, `staging.py:9` and `manifest.py:3` under C. #2741 cuts those doc sections because they restate `workspace.py` and `manifest.py`. I followed #2741: the code holds the workspace contract and the manifest fields, so those citations drop. #2742 also put `parity_matrix/cell_runner.py:12` under C. #2740 moves the cross-engine matrix tolerances into that fixture's README, so the row re-points there as a math receipt.
- **Merged from:** #2742 C, #2743 C, #2741 kept why 4 (with the mission-critical decisions).

### 5. New — Python owns canonical math; other copies are parity-tested mirrors

- **Check:** ADR 0031 has one rationale sentence, "Python service remains the authority for mathematical input/output" (`0031:46`). Its Decision is about transport, not where math lives. No ADR in force records the duplicate rule or the named exceptions. Now that `docs/math-sources-of-truth.md` is cut (owner ruling), each provenance block plus this ADR become the only record.
- **Decision:**
  - There is one canonical implementation per math concept, and its provenance block (`Formula` / `Reference` / `Canonical implementation` / `Validated against`) is the record.
  - Python is the canonical layer by default.
  - A copy in .NET or Angular is a labelled, non-authoritative mirror. It exists only for a named reason (latency, layer locality, vendor parity), and it carries a parity test that names the canonical file.
  - The one named exception is .NET FIFO lot accounting (`PositionEngine.cs`), because its data lives in EF/Postgres and runs inside DbContext transactions (F-0010).
  - The Frontend Black-Scholes helper is a frozen render-only mirror under `test_bs_cross_engine_parity.py`.
- **Conflict to settle (CLAUDE.md "when conflicts arise, surface them"):** `CLAUDE.md:22` (#5) says "Math may live in any layer that fits the use case". `AGENTS.md:59` (#5) says "Python owns all math" and that .NET or Angular computing math "is a bug". The FIFO exception's own comment rests on the CLAUDE.md wording (`PositionEngine.cs:20-27`). The ADR must pick one. **Recommended:** Python by default, with non-Python canonical math only by a named exception recorded in this ADR. That keeps both the FIFO exception and AGENTS.md's intent. See [For the map](#for-the-map).
- **Where the why lives today:** `docs/audits/computational-fidelity-2026-04-22-addendum.md:173-215` (§5 "Option A — Python owns all math", which #2740 cuts); `docs/math-sources-of-truth.md:12` (canonical is Python unless justified), `:232` (the F-0010 FIFO row) and `:355-361` (known non-compliance); `docs/architecture/numerical-authority-migration-plan.md` (#2741 cuts it); `Backend/Services/Implementation/PositionEngine.cs:20-27`; `Frontend/src/app/utils/black-scholes.ts:1-14`.
- **What waits on it:**
  - Comment rows `Backend/Services/Implementation/PortfolioRiskService.cs:280`, `PositionEngine.cs:26`, `Frontend/src/app/utils/black-scholes.ts:7` and `PythonDataService/tests/services/test_bs_cross_engine_parity.py:14` (#2743 A).
  - **Rule-file citations lean on this:** about 20 comments citing "CLAUDE.md guiding philosophy #5" or "single-source-of-truth" by name (for example `marketable_limit.py:22`, `decision_clock.py:70`, `broker_configuration/runtime.py:16`, `routers/broker_v2_gallery.py:106`, `services/broker_v2_panel/panel_chart_data_source.py:115`). The three `AGENTS.md` "Python owns all math" comments go here too (`recovery_reduction.py:835`, `research/recency/stats.py:19`, `schemas/alpaca_clerk_sqlite.py:520`). #2742 re-pointed those three to ADR 0031; re-target them here.
  - Doc cuts: `docs/math-sources-of-truth.md` (owner cut), `numerical-authority-migration-plan.md` (#2741; it also waits on #2710's `runRuleBasedBacktest` cut) and the computational-fidelity addendum (#2740).
- **Merged from:** #2743 A, the coordinator's CLAUDE.md #5 item, and the `math-sources-of-truth.md` cut.

### 6. New — Numerical rigor: strict-float equivalence, receipts and accepted departures

- **Check:** no ADR in force records the numerical-rigor rule. ADRs cite it only in passing (`0004:184` for timestamps). It lives in `.claude/rules/numerical-rigor.md` and CLAUDE.md philosophy #1–#4.
- **Decision:**
  - A port is done only when a golden fixture with attribution and a tolerance-pinned test prove it against its reference. The map's locked rule says the same: "math port paperwork = golden fixture (with attribution) + tolerance-pinned test".
  - Strict float (`atol=1e-9, rtol=0`) is the default level; bit-exact when the math allows it, behavioral only with owner approval.
  - Every float comparison states explicit tolerances, which are loosened only after the divergence is classified as precision.
  - Warmup and accumulation order match the reference.
  - Divergences are classified with the eight-category reconciliation taxonomy, kept in lockstep with `DivergenceCategory` in `app/research/parity/qc_reconciler.py`.
  - The port is sovereign: no runtime call to the reference.
  - **Accepted departures** are named in their module docstring with a kept reference note as the receipt. The standing ones:
    - lake prices are deci-cent half-up, our own quantization and not LEAN's;
    - dividend factors take Polygon's raw `cash_amount`, not LEAN ToolBox's split-adjusted input;
    - the Data Lab indicator warm-up is bounded at about 0.01 pt on oscillators and 1e-4 relative on price scale (#2611).
- **Where the why lives today:** `.claude/rules/numerical-rigor.md:7-160` and `CLAUDE.md:18-21`. For the departures: `PythonDataService/app/data_lake/lean_writer.py:66-80` with `docs/references/lean-deci-cent-encoding.md`; `data_lake/factor_files.py:35-42` with `docs/references/lean-factor-file-dividend-pricing.md` and the golden `lean-factor-file-aapl`; `services/indicator_warmup_policy.py:40-58` with `docs/references/data-lab-indicator-warmup.md`.
- **What waits on it:**
  - **Rule-file citations lean on this:** about 42 comments citing `numerical-rigor.md` in app code (#2742), for example `order_evidence.py:96-103` (the `FILL_PRICE_DRIFT` basis). The "Sovereign over the math" citation at `engine/edge/regime_clustering.py:9` also goes here.
  - Comment rows `data_lake/lean_writer.py:73`, `data_lake/factor_files.py:41` and `services/indicator_warmup_policy.py:49` (#2742 D).
  - No doc cut is blocked. The three notes stay as receipts (#2714), and `.claude/rules/numerical-rigor.md` can slim under #2715 once the ADR holds the why.
- **Merged from:** the coordinator's `numerical-rigor.md` item and #2742 D. D's three departures are the rule's own "accepted divergence" clause, so a separate ADR would only list them again.

### 7. Amend ADR 0022 — Accepted deviations from the int64-ms wire rule; the admissible range

- **Target:** ADR 0022 (Accepted 2026-07-05, not cut). It already records representation, the calendar authority, liveness and display (`0022:20-32`).
- **Decision:**
  - Name the accepted deviations in one list:
    - Polygon news `published_utc*` *filter* parameters stay vendor date strings, because whole-day semantics have no ms form; returned values are still canonicalized.
    - The Data Lab keeps its string inputs, and its never-used int64-ms branch goes (owner ruling ☆ on map #2700).
    - An owner-facing CSV may carry one display-only wall-clock column, named for its zone, beside an always-present `unix_ts` (owner, 2026-09-19).
    - The LEAN on-disk format already has its own record, ADR 0049 §1a (`0049:36-41`).
  - Every `*_ms` schema bound is `MAX_TIMESTAMP_MS` (end of 9999-12-31 UTC), not `2**63 - 1`, because the published contract must state the bound it enforces (#1936).
- **Where the why lives today:** `PythonDataService/app/routers/news.py:14`; `.claude/rules/temporal-rigor.md:30-37` (admissible range) and `:59-67` (CSV column); map #2700 Notes, ☆ Data Lab.
- **What waits on it:**
  - The comment row `routers/news.py:14` (#2742 I).
  - **Rule-file citations lean on this:** about 40 comments citing `temporal-rigor.md` re-point to ADR 0022 as a group, or drop where they only say "int64 ms UTC". `services/polygon_client.py:349` cites "CLAUDE.md philosophy #4" for time, but time is #6; it re-points here too.
  - No doc cut is blocked.
- **Not owed (re-point):** the rule's live-subscription relaxation, under "Finite ingestion vs. live subscriptions" (`temporal-rigor.md:69`), is already recorded in ADR 0053 §14 and its #2376 amendment (`0053:105-109`): post-emit corrections and late prints are ignored and counted, and an earlier timestamp stays fatal. Comments citing that part re-point to ADR 0053.
- **Merged from:** #2742 I and the coordinator's `temporal-rigor.md` item.

### 8. New — Volatility ownership and IV recording

- **Check:** no ADR in force mentions the IV recorder, Quartz, or IV ownership (grep for `iv_recorder`, `quartz`, `implied vol`). The recorder is opt-in and stays (☆ "not dead"), and the math stays (◇).
- **Decision:**
  - Python computes IV, re-solving rather than trusting Polygon's `implied_volatility` (§7.1). VIX-style IV30 is primary and parametric is the alternate (§7.2).
  - The opt-in .NET Quartz host owns the capture cron and its slot schedule, not Python in-process (§7.5–§7.6).
  - Samples go to a JSONL store until a Postgres burn-in (§7.4).
  - Recorder data is never forward-filled (§7.8), and there is no Polygon-IV fallback tier (§7.10).
  - When `health_score` is missing, edge reads fall back to recorded IV under an imputed health prior (§7.11), with `confidence_floor = 0.1` (§7.7) and `quality_score = 1 − half_spread / mid` (§7.9).
- **Where the why lives today:** `docs/architecture/iv-ownership-research.md:841-1030` (§7.1–§7.11), cited by `Backend/Configuration/IvRecorderOptions.cs:6`, `PythonDataService/app/services/iv_recorder.py:8` and `app/engine/edge/confidence.py:40`.
- **What waits on it:**
  - Comment rows `services/iv_recorder.py:8` (#2742 E), `Backend/Configuration/IvRecorderOptions.cs:6` and `tests/routers/test_edge_recorder_fallback.py:5` (#2743 B), and `routers/edge.py:375`, which cites the §8 feedback log that #2741 cuts and should re-point here. `engine/edge/confidence.py:4` and `:40` may re-point here; #2742 judged the other volatility rows drop.
  - Doc cuts: §7 of `iv-ownership-research.md`. The doc keeps §6 tolerances, §11 sources and Appendix A, because `Frontend/scripts/generate-bs-parity-fixture.py:4` and `black-scholes.parity.spec.ts:3` cite §6.
- **Merged from:** #2742 E, #2743 B, #2741 kept why 6.

### 9. New — Research run identity and sealed run inputs

- **Check:** ADR 0057 D5 *uses* `params_hash` for redelivery (`0057:15`). ADR 0056 D3 freezes one data snapshot and one code identity per study (`0056:13`). ADR 0061 treats a validated configuration as an identity (`0061:36`). None defines run identity itself.
- **Decision:**
  - A research run's identity is a canonical-JSON SHA-256 over a fixed field set, with stated exclusions. It includes `ENGINE_VERSION`, which is bumped when engine semantics change, and it falls back to `data_root_revision` as stated.
  - `params_hash` is the one cell identity shared by Recency, Grid Search and Walk-Forward (D11).
  - A trade's evidence fingerprint includes the fill model, commissions and code revision, never params alone (D16).
  - From the artifact seam: the run ledger stays immutable and hash-addressed (`ledger.json`), while other phases persist mutable configs. Hashing is opt-in per phase, so existing replay addresses stay byte-stable.
  - Model output enters a run only as a precomputed, content-hashed prediction set read by the spec `prediction` primitive. There is no live model inference inside a run.
- **Where the why lives today:** `docs/references/run-ledger.md` (hashing rationale); `docs/superpowers/specs/2026-08-16-recency-chart-design.md` D11/D16, which #2740 cuts; `docs/architecture/research-artifact-seam.md:76-77` (decisions 1–2); `docs/ml-predictions-authority.md` and `docs/superpowers/specs/2026-05-09-ml-prediction-as-data-v05-design.md`, which #2740 cuts; `PythonDataService/app/research/ml/artifact.py:1-20`.
- **What waits on it:**
  - Comment rows `research/runs/__init__.py:12`, `descriptor.py:30`, `:59`, `hashing.py:62`, `ledger.py:41`, `research/sweep/grid.py:15`, `research/recency/fingerprint.py:12` (#2742 G) and `research/ml/artifact.py:19`, `:20` (#2742 F). `research/artifact/descriptor.py:3`, which cites the seam doc, can point here or drop.
  - Doc cuts: **the recency-chart spec and the ML v0.5 spec cannot go until this lands**, because D11/D16 and the "predictions as data" why live only there. Also `research-artifact-seam.md` (slim, then cut), the decision parts of `run-ledger.md`, and `ml-predictions-authority.md`.
- **Merged from:** #2742 G and F, and #2741 kept why 3 (seam decisions 1–2 only). F belongs here because a prediction set is one more hashed run input that rides the same `hash_payload`.
- **Liveness:** the spec runner is live. #2706 cuts only `GET /api/spec-strategy/schema`. So the `prediction` primitive and `app/research/ml/` are reachable, and the F part stands.

### 10. New — Research verdict rules

- **Check:** ADR 0056 D5 covers only the walk-forward *study* verdict (`0056:15`). It says the spec-path walk-forward "is untouched" (`0056:22`). No ADR records the other rules.
- **Decision:**
  - Signal graduation uses Stage 0–3 thresholds adopted from the 2026-04-30 external methodology review.
  - The alpha-decay test needs a minimum number of folds.
  - The spec-path walk-forward uses a fixed split policy and a compounded, not rebased, combined out-of-sample curve.
  - The run verdict is a fixed completeness contract: 17 sub-scores with fixed weights and no dynamic reweighting.
- **Where the why lives today:** `docs/signal-engine-authority.md` §4–5, served byte-identical as `Frontend/src/assets/docs/signal-engine-methodology.md`; `docs/references/walk-forward.md`; `docs/references/reconciliations/engine-lab-runs-75-76-statistics-validation-plan.md`, "Product decisions".
- **What waits on it:** comment rows `research/signal/graduation.py:3`, `:4`, `:35`, `:290`, `research/signal/walk_forward.py:31`, `research/walk_forward/__init__.py:16` and `services/run_verdict_service.py:7` (#2742 H). Doc cuts: the decision parts of `walk-forward.md` and of the statistics-validation plan. The served methodology doc follows the open owner question on served docs (#2741, For the map 1).
- **Merged from:** #2742 H only.

### 11. Amend ADR 0043 — Per-run build evidence is dynamic, file-backed run evidence

- **Target:** ADR 0043 (Accepted, not cut). It records the seal structure and keeps the v1 `configuration_hash` untouched (`0043:81`), but not why per-run build evidence stays out of SQLite.
- **Decision:**
  - The running build digest and qualification-receipt identity are *dynamic run evidence*, recorded per run under `live_state/<instance>/run_build_evidence/<run_id>.json`. They are not semantic bot identity.
  - They stay file-backed, and they are excluded from `config_json`, so build content can never perturb `configuration_hash` and every seal built on it.
  - Moving them into SQLite is a new decision, not a cleanup.
- **Where the why lives today:** `docs/architecture/engine-authority-map.md:149-172`, which cites the sealed-program PRD §11.3 (#2740 cuts that PRD).
- **What waits on it:** no comment rows. Doc cut: the "Per-run build evidence is file-backed on purpose" section of `engine-authority-map.md`. #2741 keeps it until this lands.
- **Merged from:** #2741 kept why 5. It is low priority because the kept map section still holds the why.

---

## Duplicates merged

| Merged entry | Input entries it absorbs |
|---|---|
| 1. ADR 0059 amendment | #2742 A |
| 2. ADR 0036 amendment | #2742 B; #2741 kept why 7 (custody money normalization) |
| 3. ADR 0035 amendment | #2740 owed 1 (VM-local storage); #2743 D (snapshot stream, ADR 0028); #2741 kept why 1 (resolved as a re-point to ADR 0035's annex) |
| 4. LEAN sidecar ADR | #2742 C; #2743 C; #2741 kept why 4 (with the mission-critical D2/D3/D5/D10) |
| 5. Math-authority ADR | #2743 A; coordinator item CLAUDE.md #5; `AGENTS.md` #5; the `math-sources-of-truth.md` cut |
| 6. Numerical-rigor ADR | coordinator item `numerical-rigor.md`; #2742 D (accepted departures) |
| 7. ADR 0022 amendment | #2742 I; coordinator item `temporal-rigor.md` |
| 8. Volatility ADR | #2742 E; #2743 B; #2741 kept why 6 |
| 9. Run identity ADR | #2742 G; #2742 F (ML predictions); #2741 kept why 3 (seam decisions 1–2) |
| 10. Verdict rules ADR | #2742 H |
| 11. ADR 0043 amendment | #2741 kept why 5 |

## Dropped or re-pointed (not owed)

| Input entry | Verdict | Why |
|---|---|---|
| #2741 kept why 2: ADR 0060 amendment (profile contract §1, §3, §9.2, §9.4) | **re-point** | ADR 0060 already records §1: secrets never cross (`0060:39`), owner and actor are server-resolved and request schemas are closed (`0060:64`). It records §3, the closed code-owned slot allowlist (`0060:164`). ADR 0062 §3 records §9.4: `clerk_id` is backend-issued and never minted (`0062:67`). §9.2 (desk identity truth layers) has no non-spec Frontend reader: `effective_choice` appears only in specs and generated `broker.types.ts`. The docs handoff judges it. |
| #2741 kept why 8: ADR 0058 (`engine-persistence-authority.md` "Why") | **re-point** | ADR 0058 D3 already records "one converter, one write" for the engine, LEAN and spec producers (`0058:13`). |
| #2741 kept why 9 (optional): ADR 0038/0018, the retired IBKR actuation | **re-point** | ADR 0038 is the one bot control plane, and ADR 0062 records that the IBKR retirement removed "its control UI and order authority", with provider changes needing a new owner decision (`0062:19-44`). `ibkr-integration-tdd.md` stays as a kept IBKR feed doc anyway. |
| #2741 kept why 10 (only if the owner wants it): edge §3 anti-leakage | **drop** | No rule exists and the promised CI guard was never built (no hit for `no_leakage`). It goes with `edge-feature-design.md` unless the owner asks for it. |
| #2740 owed 2: ADR 0034, the Paper override is permanent | **drop** | ADR 0034's mode-tiered amendment records the Paper tier and its reason (`0034:79-119`: evidence gates "relax by tier" because Dry Run and Paper "carry different consequences"). Under ADR 0039 an Accepted decision stands until a new decision supersedes it, so "permanent" adds nothing enforceable. |
| #2741 kept why 3: seam decisions 3–6 (scan listing, phase-owned types, exception bases, descriptor-bound store) | **drop** | Code structure only; the code states it (`research/artifact/`). Decisions 1–2 went to entry 9. |
| #2742 B row `clerk/synthesized_orders.py:25` (average cost for `sim:`/`shadow:`) | **drop** | The docstring states the convention, which mirrors the broker's own average-cost position. #2741 cuts its note (`synthetic-broker-position-projection.md`). |
| #2742 B rows `folds.py:946`, `:958` and `facts.py:487` | **re-point** | To ADR 0036 D1 and ADR 0030 (`0030:102`); see entry 2. |
| #2742 C rows `workspace.py:13`, `staging.py:9`, `manifest.py:3` | **drop** | #2741 cuts those sections because they restate the code. See entry 4. |
| #2742 C row `parity_matrix/cell_runner.py:12` | **re-point** | To the cross-engine golden-matrix fixture README (#2740 moves the tolerances there). A math receipt, not an ADR. |
| #2712 hazard: ADR 0003 is the only record of IBKR error 420 / Trusted IPs | **not an ADR** | It is an outside fact. #2712 moves it into `docs/ibkr-integration-authority.md` if it still holds. |
| #2712: ADR 0041 Decision 6 | **not an ADR** | `test_vocabulary_snapshot.py` states the rule itself (#2712 hazard 6). |
| #2740 Not reviewed: the panel audit's P2-2 ("trusted-local control channel, not user authorization") | **already recorded** | ADR 0060: "The shared secret authenticates a *control context*, not a person" (`0060:62`). |
| Broker-provider boundary (coordinator item) | **re-point** | ADR 0062 §"Retained market-data provider — owner decision 2026-09-16" (`0062:19-44`), which CLAUDE.md already links. |

## Rule-file citations: where each one points

The owner ruled that comments cite ADRs, never rule files. This is where each standing rule's citations go. A later ticket judges the rows against this.

| Rule cited | ADR in force that records it | Owed |
|---|---|---|
| `CLAUDE.md` #5 single source of truth (~20 comments, many by number); `AGENTS.md` #5 "Python owns all math" (3) | none (ADR 0031:46 has one rationale sentence) | **Entry 5** |
| `CLAUDE.md` #1–#4 and `.claude/rules/numerical-rigor.md` (~42), including "Sovereign over the math" | none | **Entry 6** |
| `.claude/rules/temporal-rigor.md` and `CLAUDE.md` #6 (~40) | ADR 0022; live-subscription redelivery is ADR 0053 §14 (`0053:105-109`); the LEAN on-disk deviation is ADR 0049 §1a | **Entry 7** for the deviations and the range |
| `CLAUDE.md` "Required broker-provider boundary" | ADR 0062 `:19-44` | none |
| "Operator copy is authored here, not in the client (CLAUDE.md hard rule)" (`schemas/alpaca_live_verdict.py:38`, `broker/alpaca/active_binding.py:57`) | ADR 0035 D12, "the frontend derives no safety" (`0035:188-193`); ADR 0036 D2 for numeric boundaries; ADR 0014 for backend-rendered narratives | none |
| `.claude/rules/python.md` snake_case (`schemas/backtest_runs.py:7`) | ADR 0058 D6 records the deliberate camelCase exception (`0058:16`) | none; other `python.md` citations drop |
| `CLAUDE.md` coding hard rules (no `print()`, no silent catch, structured logging) | none, and none is owed | Not decisions; the rules and lint own them, so the citations drop |

## Hazards the handoff must carry

1. **ADRs land before the docs they cite are cut.** Under #2742 hazard H5, an "ADR owed" comment keeps its citation until its ADR is accepted. These planned cuts wait on an ADR here:
   - `lean-sidecar-mission-critical.md` (entry 4)
   - ADR 0028 (entry 3)
   - the clerk paper-soak report (entry 3)
   - `math-sources-of-truth.md`, `numerical-authority-migration-plan.md` and the computational-fidelity addendum (entry 5)
   - the recency-chart spec and the ML v0.5 spec (entry 9)
   - `custody-budget-money.md` (entry 2)
2. **Docs contract.** Each new ADR adds a row to `docs/doc-authority.md`, and `scripts/check_documentation_contract.py` must stay green while it lives. Under ADR 0039 a new ADR starts `Proposed` until the owner accepts it. An owed comment re-points only after acceptance.
3. **Hashed files.** Any re-point inside a Signal Program build-proof source (`app/engine/strategy/program_sources.py`; for example `app/lean_sidecar/trading_calendar.py` and the strategy algorithms) changes a program digest. Those comment edits ride the one re-qualification PR (#2742 H1).
4. **Money-path files.** Entries 1–3 re-point comments in clerk SQLite and Alpaca files. Change comments and docstrings only, and re-run the clerk SQLite and Alpaca suites (#2742 H7).
5. **Served copies.** Entry 10's source doc has a byte-identical served copy. Any slim edits both sides in one commit (#2741 hazard 1).
6. **Sealed citations.** `alpaca-live-envelope.md` is cited by the PNL-001 golden fixture. Entry 1 must never move or rename it (#2741 hazard 2).

## For the map

- **Rule-level question for the owner: where does math live?** `CLAUDE.md:22` says "Math may live in any layer that fits the use case". `AGENTS.md:59` says "Python owns all math", and that .NET or Angular computing math "is a bug". The .NET FIFO engine relies on the first. Entry 5 must pick one. Options:
  - **(Recommended)** Python by default; a non-Python canonical copy only by a named exception in the ADR (today, .NET FIFO lots).
  - Python only; move FIFO lot accounting to Python.
  - Any layer, with a parity test for every duplicate.
- **Debt the `math-sources-of-truth.md` cut orphans.** Its "Known rule-5 non-compliance" list (`:355-361`) and `legacy-ok-pending-parity` rows track debt, not decisions:
  - .NET `SnapshotService.cs` Sortino, CAGR and Calmar have no parity test (F-0011, `:170-174`).
  - The hardcoded `r = 0.043` sits in five production sites besides the FRED fallback.
  - `rule_based_backtest.py` is a deferred migration.

  An ADR is the wrong home for these. File them as issues, or accept them, in the same PR that cuts the registry.
- **Conflicts between input lists resolved here.** `workspace.py:13`, `staging.py:9` and `manifest.py:3` drop, following #2741. `cell_runner.py:12` re-points to the fixture README, following #2740. `synthesized_orders.py:25` drops, following #2741. #2742's re-point of the three `AGENTS.md` comments to ADR 0031 is re-targeted to entry 5.
- **Graduation:** this list is ready to slice into ADR-writing handoffs. Entries 1–3 are small amendments to money-path ADRs and should go first, before any cutting PR that drops the docs listed under hazard 1.

## Not reviewed

- **ADR bodies, clause by clause.** Targets and re-points were checked by reading the cited sections (ADRs 0022, 0031, 0034, 0035 D12 and annex, 0036, 0043 §1, 0045, 0049 §1a, 0053 §14, 0056, 0057, 0058, 0059 D1/D5/budget, 0060, 0062 §3 and provider section). Other ADRs were checked by grep only.
- **Rule-file citations outside `PythonDataService/app/`.** The counts (about 110) come from #2742 and were not re-run. Backend, Frontend, scripts and test comments that cite `CLAUDE.md`, `AGENTS.md` or `.claude/rules/*` were not categorized.
- **ADR 0053 §14 against every clause** of the rule's live-subscription relaxation. I checked post-emit corrections, late prints and earlier-timestamp fatality, but not the before-emit recompute clause.
- **Bodies not read:** `docs/references/run-ledger.md`, `docs/references/walk-forward.md`, `signal-engine-authority.md` §4–5 and the statistics-validation plan's "Product decisions". Entries 9 and 10 rest on #2742's reading of them.
- **Whether entry 10's graduation and run-verdict code is reachable from a live route.** No dead-code list cuts it; I did not trace callers.
- **§9.2 liveness** rests on a grep for `effective_choice` in non-spec Frontend code (none outside generated types).
