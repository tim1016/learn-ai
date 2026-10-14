# Clean-up — Options Backtest: engine, routes and page (#3154)

Findings for ticket [#3154](https://github.com/tim1016/learn-ai/issues/3154) under map [#3146](https://github.com/tim1016/learn-ai/issues/3146) (tracker #3145). **Read at `d1fe80ce`** (origin/master, 2026-10-14 ~13:10 CDT; the module's last change before that was the re-layout, #3139–#3141). This file lives on the throwaway branch `research/cleanup-options-backtest` and is never merged. The delete-only cut is [#3185](https://github.com/tim1016/learn-ai/pull/3185).

## Result

- **No P0, no P1.** Seven readers covered the module by sub-package, and I re-checked every cut and every claim that could have been a P1. Nothing fails open, nothing shows the owner a wrong figure, no stored data silently corrupts. The module is young and tidy: the cut is 33 lines.
- **One cut PR, #3185:** 2 Python pieces (a derived property nothing derives; an injection parameter nobody passes) and 17 Frontend pieces (12 unused type aliases, one dead branch with its spec row, one dead CSS rule, three unused test-fixture parameters/constants). No contract change.
- **For handoff:** 9 small traced-or-reproduced P2/robustness/hygiene slices (H1–H9), a lens-6 test-bar list (about 60 duplicate or weak tests, each naming its surviving assertion), about 30 wire fields served and never read (cross-stack: needs serial OpenAPI + `broker.types.ts` regeneration), and 14 owner rulings.
- **Feature coverage:** about 95 feature rows over the seven slices, none missing. The partial ones are named in their sections: the price step has no UI field (F2-D3), a History-opened run without a setup shows no fee line (F2-D1), and opening a run whose stored summary is damaged answers a bare 500 (B3-D). The unverified ones are by design: stops, momentum, re-entry, half day and estimated fees against real vendor bars (ADR 0078 d12 says so), and the "no broker import" boundary, which no test enforces.

## Method

Seven read-only readers, split by sub-package (never by lens), each briefed with the lenses, the Sacred list, the kill lists *with their comments* (none names this module; it was built after that sweep), the pointers and the method rules from earlier tickets (a name is a lead not a verdict; `rg` hides `Frontend/src/app/api/*.types.ts`; read the survivor's assertion before calling a test a duplicate):

| id | slice |
|---|---|
| B1 | engine core: `replay`, `run`, `setup`, `picking`, `fills`, `fees`, `cash_ladder`, `cover_book`, `market`, `needs`, `classification`, `phrases`, `describe`, `limits`, `results`, `statistics`, `capital_evidence`; their tests; golden fixtures; ADR 0078 |
| B2 | data side: `fetching`, `data_check`, `lake_check`, `lake_data`, `chain_download`, `past_chain`, `data`, `service`; routers `options_backtest`, `options_backtest_chain`; schemas; wiring (`main.py`, `jobs/phases.py`, `JobsApi.cs`) |
| B3 | storage and detail: `saved_runs`, `saved_setups`, `run_json`, `run_detail`, `run_stamps`, `comparison`, `trade_export`, `session_*`, `sessions`; routers `options_backtest_runs`, `options_backtest_run_detail`; schemas; research DDL |
| F1 | Frontend page root, `run/`, `controls/`, `testing/` |
| F2 | Frontend `cards/`, `history/` |
| F3 | Frontend `past-chain/` |
| F4 | Frontend `run-detail/` |

Their reports are appended below as sections B1–B3 and F1–F4, unchanged except where the **Driver's reconciliation** section says otherwise.

## Evidence

| run | result |
|---|---|
| All Python tests of the module (`tests/options_backtest` + the 11 router files) on a throwaway Postgres (`POSTGRES_URL_IS_EPHEMERAL=1`), at `d1fe80ce` | 3,652 passed, 0 skipped, 32 errors — the known `relation "DataLakeArtifacts" does not exist` (EF-owned catalog table, empty database) |
| Same files without Postgres (readers B2 + B3) | 724 + 49 skipped and 437 + 97 + 241 + 63 skipped: every skip is `POSTGRES_URL not configured`. PR CI has no Postgres, so those ~160 tests run only in the daily |
| Frontend specs of the module (readers) | F1 731 passed (45 files), F2 230 (History 56 + 174 page-spec tests that exercise the cards), F3 387 (18 files), F4 475 (20 files); 0 skipped; vitest printed the worktree root |
| `python scripts/export_openapi_contract.py --check` | exit 0 (snapshot == Pydantic) |
| `ruff` lens set (`S110,S112,BLE001,E722,T201,DTZ003,DTZ004,DTZ005,DTZ007,F401,F841,ERA001`) | 4 `BLE001`, all deliberate catch-and-carry/record (`fetching.py:600`, `:654`, `service.py:231`, `:255`) |
| `eslint --max-warnings 0`; `tsc --noUnusedLocals` | clean over the slice |
| After the cut: `tsc -p tsconfig.json` and `tsconfig.spec.json`; eslint on the touched files; `ruff check app/ tests/`; 22 Frontend specs (447 tests); 12 Python files on Postgres (754 passed, same 32 errors) | clean |

Parity tests really run: `test_picking_parity.py` 218 cases, `test_cover_book_parity.py` 8, `tests/broker/alpaca/clerk/test_account_picture.py:476`; each has a vacuity guard. The two golden fixtures are hash-validated by `tests/fixtures/test_golden_manifest.py` (10 passed).

## The cut — PR #3185

| file | piece | evidence |
|---|---|---|
| `app/options_backtest/data_check.py` | `SessionCheck.trading_date` | no receiver in `app/` or `tests/`; docstring says "Derived, never stored or sent"; nothing derives it |
| `app/options_backtest/fetching.py` | `ensure_run_data(data=)` | the one app caller omits it; no test passes it (`service.py:280`'s `data=` is `check_data`'s) |
| `run-detail/run-detail.types.ts` | `TradeFill`, `TradeMark`, `SettingDifference`, `RangeDifference`, `SharedSession`, `RunTotals` | `git grep -w` over `Frontend`, generated types included: own definition only |
| `past-chain/past-chain.types.ts` | `ListingState`, `PriceState`, `UnderlyingState`, `DownloadReason` | same |
| `options-backtest.types.ts` | `Refusal`, `RunHeadline` | same |
| `setup-problems.ts` + spec | `FIELD.name`, the `path`/`name` branch of `keyOf`, one spec row | `problemsFromDetails` has one production caller (`setup-summary-checker.ts:90`, `describe`, which has no path parameter); a save-time 422 goes `SetupInvalidError` → `reasonOf`/`describeDetail` and never reaches `keyOf`; no template reads a `name` field problem |
| `run-detail/session/session-tables.scss` | `.rows .none` | neither template that loads the sheet uses the class |
| `past-chain/testing/fixtures.ts` | `NOON_MS`; `ContractWanted.expiry`; `occSymbol`'s `root` parameter | no caller |

## Driver's reconciliation (what changed between the readers' reports and the cut)

- **Held out of the cut by the map's lens-2-only rule** (the map: "covering only lens 2; everything outside lens 2 stays a finding for handoff"): every test-bar duplicate and weak test (lens 6), the misleading comment at `replay.py:575-576`, `CONTEXT.md:1964`, `trade_export.py:4`, the ADR 0022 amendment clause, and the dead `File.prototype.text` polyfill in `options-backtest-page.duration.spec.ts`. They are rows in D below.
- **Test-only production code left, per the #3150 precedent ("five test-only methods (leave)")** — the tests that call them prove behaviour that stays, so cutting means rewriting tests, not deleting dead ones: `saved_runs.list_runs` (B3, F2), `trade_export.trades_csv` (B3), `picking.pick_contract` (B1; also named in `engine-authority-map.md:139`), `results.SessionOutcome.exit_counts`/`event_counts` (B1), `statistics.Ratio.exact` (B1: the exactness oracle of money-statistics tests), `saved_runs.read_result` (B3: the "Validated against" oracle of the run round trip), `fetching.DownloadReport.sessions_prepared` (B2).
- **`LakeRunData(lake_root=)` (B2-B2) left:** a deliberate, tested seam from the first commit (`4983d282`, "an alternate mount of the lake"); cutting it orphans `lake_root` parameters in `app/data_lake/option_bars.py`, `option_listing.py`, `option_lake.py` that belong to the data lake. Owner ruling: keep the seam or drop it together with its two tests and those three parameters.
- **`.band h3:focus-visible` (F2-B)** is a CSS rule that cannot match (the heading is never focusable) but it is a focus style; the owner's 2026-10-12 accessibility ruling says to skip focus findings in fixes. Left.
- **`RunStatus`** (F1-B1 optional) left: one spec imports it.
- **No thermo reviewer was run on #3185:** 4 lines added, delete-only, no provenance block, golden fixture, order, custody or arming path touched (AGENTS.md → Gates). It ships on CodeRabbit plus green CI.
- **B2's "underlying capture says `fetched` without reading the day back" (`chain_download.py:185-196` vs `fetching.py:388-395`)** — I read both. The run path reads the day back and raises `NOT_IN_THE_LAKE`; the chain-download item returns `FETCHED` straight after `materialize_engine_run`, which itself verifies its artifacts are on disk (engine-authority-map). The remaining gap is a stored file with no regular-session bar. P2, traced not reproduced; self-corrects on the next press.

## Handoff list for #3159

### Fix slices (each PR-sized; severity is P2 or robustness unless noted)

| # | what | where | lens | evidence |
|---|---|---|---|---|
| H1 | `RunPanel` keeps the previous run's "could not be read" notice after the run id changes (`readFailure` is not reset) | `run/run-panel.component.ts:100-107,131` | 3 | F1-D1, traced; `run-panel.spec.ts:338-364` cover `record`, not `readFailure` |
| H2 | History "Use this setup" is a silent no-op when the session clock was never read (the other two setup-opening paths are gated) | `options-backtest-page.component.ts:339-342` | 3 | F1-D2, traced |
| H3 | The past-chain download's underlying capture reports `fetched` without reading the day back | `chain_download.py:185-196` | 3 | B2 + driver read |
| H4 | The download request's `contracts: list[str]` has no `max_length`; every symbol is parsed on the event loop before the 60-contract cap | `schemas/options_backtest_chain.py:263`, router `:176` | 5 | B2 |
| H5 | `GET /runs/{id}` answers a bare 500 (`{"error":"'sessions_traded'"}`) for a complete run whose `summary_json` lacks a key; the session route answers a coded 500 and the run still opens | `saved_runs.py::get_run` → `run_json.summary_from_json` | 3/5 | B3, reproduced by `B3/repro_open_run_500.py`; nothing writes such a shape today |
| H6 | `statistics.py:415` silently skips a `PICK_REFUSED` event with no reason (`_refusal_of` raises for entry refusals); two generated-scene helpers `return None` and the test `pytest.skip`s ("not reached by construction") | `statistics.py:415`; `test_replay_selling_invariants.py:138,304,316`, `test_capital_evidence_path.py:61` | 3/6 | B1 |
| H7 | Port a few DB-gated refusal-code assertions (`SETUP_CANNOT_BE_READ`, `RUNS_OF_DIFFERENT_UNDERLYINGS`, `SESSION_NOT_IN_RUN`, `LEG_NOT_IN_RUN`, `STORED_SESSION_INCONSISTENT`) onto `_fake_connection.py`, so PR CI fires them (today only the daily does) | `tests/routers/test_options_backtest_run_detail_*` | 3/6 | B3 |
| H8 | Comments and docs: the `replay.py:575-576` comment says `close is None` "always holds here today" — it does not, and removing the guard moves a waiting momentum leg's hit from minute 15 to 21 (`B1/guard2.py`); `CONTEXT.md:1964` says refusals are "not counted in the summary yet: #3109" (they are); `trade_export.py:4` cites a rule file; ADR 0022 amendment (f) still says the Data Lab past-chain analysis date "feeds OCC symbol synthesis" (deleted in #3131); `validate.component.ts:52` docblock + `validate.component.html:108-114` stub; `engine-authority-map.md:142,144` | listed | 7 | B1, B3, F3 |
| H9 | Hygiene, mechanical: two copies of the `^[A-Z]{1,5}$` literal pinned equal by a test; `ensure_run_data`'s `DownloadFailed.detail` read and logged by nothing; `.again`/`.sr-only` rules copied per component; test-helper copies (`readText` ×3, `openExample`, `going()`, `MS_PER_MINUTE`, `deferred.ts` ×5) | listed in B2-D, F1-D6/D7, F2-D7, F3-D | 7 | — |

### Test-bar slice (lens 6; delete-only; every row names the surviving assertion in its section)

About 45 tests across B1 (7 rows incl. `test_phrases.py:75-77`, `test_capital_evidence.py:137-157`, `test_describe.py:430-440`), B2 (5), B3 (10 incl. the four 30-case 422 sweeps that are subsets of `hostile_requests`), F1 (B3–B8: the tombstone at `sides.spec.ts:28`, `phases.spec.ts`, `run-result.spec.ts:252-262`, `setup-build.spec.ts:22-66`, the dead polyfill), F2 (6 History specs), F3 (10), F4 (12). Confidence per row is in the section (confirmed / probable). Accessibility-only specs are **not** in it — owner ruling, see below. One PR per frontend/backend half keeps each reviewable; order does not matter.

### Wire fields served and never read (cross-stack; needs `scripts/export_openapi_contract.py` then `Frontend/tools/openapi-codegen`, serial)

`first_testable_session_open_ms`, `last_testable_session_open_ms` (and `DataCheck.first_testable/last_testable`), per-session `session_close_ms` of a check; `Drawdown.measured_at`; `RunFigures.exits_by_reason`, `gross_profit_usd`, `gross_loss_usd`; `FillsBanner.fee_policy_kind`, `fee_per_contract_usd`; `RunDetail.calendar_identity`, `calendar_package_version` (+ NOT NULL columns, `schema.py:929-930`); `Trade.exit_group`, `multiplier`, `exit_kind`; `TradesResponse.run_id`, `limit`; `SessionResponse.run_id`; `ComparedRun.first_session_open_ms`, `last_session_open_ms`, `sessions_requested`, `fee_policy`; `SettingDifference.setting`, `leg_id`; `RunListItem.finished_at_ms`; `LimitsResponse.max_reentries_per_leg`, `max_range_sessions` (cascades to `limits.py` and its import-graph test); `PastChainContract.steps_from_the_money` (Python tests assert it), `PastChainSide.right`, `at_the_money_strike`, `PastChainDayResponse.underlying`, `PastChainExpiryTag.version`, `PastChainDownloadResponse.session_close_ms`, `PastChainContract` steps ladder in `past_chain.py:436-451`. Money-trail fields (`exit_group`, `multiplier`, `exit_kind`, gross P&L) stay unless the owner says otherwise.

### Owner rulings

1. Display zone of instants in `run-detail/` and past-chain bar closes: `mode="et"` everywhere vs the owner's local-for-fills/events preference and `temporal-rigor.md` (F4-D3, F3).
2. Price step has no field on the costs card but is in the draft, the body and the saved file: build the field or drop `price_step_usd` from the UI path (F2-D3).
3. History-opened run with no openable setup shows its figures with no fee line, though `fee_policy` is on the run (F2-D1): show it or drop the field.
4. `LakeRunData(lake_root=)` alternate-mount seam: keep or drop with its two tests and three data-lake parameters (B2).
5. Multi-press download machinery (`remaining`, `callTakes` slice, `planWords` branch, 5 tests): unreachable while both server caps are 60; keep for a lower cap or cut (F3-C1).
6. `filenameOf` starred-name / bidi / control-character branch of the CSV filename reader: the server only ever sends a plain ASCII name (F4-C1).
7. Test-only production code listed under the reconciliation (move to `tests/` or keep).
8. `slow` marks: the selling-invariants / needs-property families run 362 tests in 5.6 s on PRs and 447 in 4.9 s daily; hand-picked seeds 1/96/161 save ~5 s locally per PR (B1); `test_trade_export_batches.py:269` is `slow` on a 3.5 s test, so the bounded-memory proof runs only in the daily (B3).
9. Golden #1's `regenerate.py` "has not been run in this form" (own docstring); golden #2 has the offline end-to-end script test (B1).
10. `session_view`/export: no cap on concurrent exports and session reads (single-owner tool) (B3).
11. The run job route `POST /api/jobs-internal/options-backtest` is not under the control secret though its download spends vendor requests — already recorded in ADR 0078 "What this does not decide" (B2).
12. Accessibility-class specs and the production code that serves them (`focusHeading()` ×3, `afterNextRender` focus moves, `RunFailureComponent.listId`, History focus machinery, 17 axe tests in `past-chain-tab.a11y.spec.ts`, 11 in run-detail, ~25 s of CI time in F1's slice): listed, never cut, per the 2026-10-12 ruling. `.claude/rules/angular.md` still says "All UI must pass AXE" and now disagrees with AGENTS.md.
13. Hand-written copies of server rules on the page (`session-time.ts:20-23` `LEAST_MINUTES`, `testing/render-page.ts:54-91`, `runs-listed.ts:12` limits): add one contract test naming `setup.py`/the route, or leave (F1-D4, F2-D4).
14. Pointer, not this module: `Frontend/src/assets/docs/bot-business-manual.md:64` and `app/schemas/traded_asset_acknowledgement.py:154` say "Options have no backtest here." — possibly true of a deployed strategy's evidence, possibly stale (F1-D12).

### Pointers outside this module

- `tests/unit/data_lake/test_option_bars.py:1706,1725`, `test_option_listing.py:1361,1380`, `app/data_lake/option_lake.py:477-482`: the `lake_root` parameters whose only production caller is the seam in ruling 4.
- `shared/symbol-catalog/testing/fake-symbol-catalog.ts:70`: the one `noUnusedLocals` hit outside the slice (F1).
- `Frontend/ci-spec-durations.json:269`: `run-trades.component.spec.ts` recorded at 17.25 s in CI vs 4.7 s locally (F4); the near-5 s spec of #3123 is probably one of the axe runs (F1).

## Not covered

All seven readers state what they only skimmed in their F section. The two real-vendor-data gaps ADR 0078 d12 names (stops, momentum, re-entry, half day, estimated fees against vendor bars) are unverified by design, not by omission. No browser run of the page was done; layout claims rest on specs and the container-query declaration.

---

# Reader reports (transcribed from each reader's inline report, lightly condensed; headings demoted one level)

## B1 — engine core (17 modules, golden fixtures, ADR 0078) — read at d1fe80ce

Headline: no P0/P1. Slice tests: 700 (replay/run batch) + 1,464 (rest) = 2,164 passed, 0 skipped, none DB-gated. Lint set clean on sources, tests and fixtures; full project ruff clean. No `noqa`/TODO/FIXME/`type: ignore` in sources; only `np.allclose` calls carry explicit `atol=1e-12, rtol=0` (`test_drawdown.py`). No hardcoded session time, naive datetime, fixed ET offset or second `get_calendar` (only `app/lean_sidecar/trading_calendar.py:29`); windows via `session_window_for_date`/`expected_sessions`; expiries via `option_expiry_date`. Every member of `EventKind`, `ExitReason`, `SkipReason`, `PickRefusal`, `EntryRefusal`, `OpeningRefusal`, `CapitalInvalidKind`, `ExpirySeries`, `FillGroup` has a production producer. Comments cite issues/ADRs/"PRD #nnnn" only. ADR 0078 (+ amendments), `engine-authority-map.md:134-139` and module docstrings match code except `CONTEXT.md:1964`.

### A. Feature table (all reachable: `Backend/Jobs/JobsApi.cs:51` → `/api/jobs-internal/options-backtest` → `routers/options_backtest_runs.py` → `service._replay` → `run_sessions` → `replay_session`; routers `app/main.py:1308-1359`; page `app.routes.ts:239`)

| feature | requirement | entry | verdicts | class | evidence |
|---|---|---|---|---|---|
| Clock-driven session replay (bought legs, next-open fills, window skip) | ADR 0078 d3–4 | `replay.py::replay_session` | skipped for window / no entry bar; non-whole-minute session raises | implemented | `test_replay_entries.py` (window-fit sweep, `…underlying_did_not_trade…_skipped`), `test_replay_money.py::…not_open_and_close_on_a_whole_minute_is_refused` |
| Momentum entry, dropped entry, re-pick | d4 | `_check_momentum`, `_drop_entry` | no bar → back to waiting; no starting price done | implemented | `test_replay_entries.py::test_momentum_buys_at_the_next_opening_price…`, `…goes_back_to_waiting_and_buys_later` |
| Stops, square-off (partial/complete), exit time, marks | d4, d11 | `_stops`, `_exit_time`, `_mark_what_is_held` | sale with no bar retried; never sold → marked | implemented | `test_replay_stops.py`, `test_replay_event_numbers.py` |
| Re-entry on stop with cutoff and count | d4, OQ2/3 | `_earns_reentry`, `_reenter` | blocked by cutoff / lost for want of data | implemented | `test_replay_reentry.py` |
| Cash admission, fees, exact `Decimal`, no slippage float | d8, d10, d11 | `_judge_opening`, `fills.py`, `fees.py` | refused for cash; 100-digit `Inexact` raises | implemented | `test_replay_money.py`, `test_fills.py`, `test_fees.py` |
| Sold covered legs, cover book, collateral, atomic opening group | d1 amend (#3106) | `cover_book.admit_opening`, `_judge_opening` | refused (other side, 2nd expiry, no cover, cash, sale at 0) | implemented | `test_replay_selling.py`, `test_cover_book.py`, golden #2 |
| Protection closes + sold-leg stops | d4 amend (#3107) | `_close_with_protection` | waits when buy-back has no bar | implemented | `test_replay_selling_stops.py`; repro `guard.py` |
| Run: cash carried, slippage ladder, tolerance | d9, d11 | `run.py::run_sessions` | skipped / `SessionDataMissing` / stop raises `RunStopped` | implemented | `test_run_cash.py`, `test_run_slippage.py`, `test_run_missing_data.py` |
| Capital evidence, cash ladder, stamps | d11 amend (#3108) | `capital_evidence.py`, `cash_ladder.py`, `run.py::_CashLevel` | unfunded close ends it; marked session ends it; odd-cent cash | implemented | `test_capital_evidence.py`, `test_run_capital.py`, `test_capital_evidence_path.py` (150 + 250 generated sessions) |
| Run statistics, drawdown (parity with stock engine) | d8 | `statistics.py::summarize_run` | no trades / no sessions | implemented | `test_statistics.py`, `test_drawdown.py` (atol=1e-12, rtol=0) |
| Backtest's own contract picking | d6 | `picking.py::prepare_leg`, `PreparedLeg.pick` | refusals / incomplete listing | implemented | `test_picking.py`, `test_picking_prepared.py` |
| Picker parity with live resolver | ADR 0068 d4 | — | 218 cases collected and run | implemented | `test_picking_parity.py` (names `app/options/resolver.py`) |
| Cover-book parity with bots' cover check, requirement, account picture | ADR 0076 | — | 8 collected and run | implemented | `test_cover_book_parity.py` (names `structure_check.py`, `requirement.py`); `tests/broker/alpaca/clerk/test_account_picture.py:476` |
| Contracts a session can need | d6, d18 | `session_needs` | skip reasons | implemented | `test_needs.py`, `test_needs_property.py` (200 + sell family) |
| Setup validation and stored-bytes stability | d23 | `setup.py` | refused | implemented | `test_setup.py`, `test_stored_setup_reasons.py` |
| Setup sentences, result statements | d1, d24 | `describe.py`, `phrases.py` | notices | implemented | `test_describe.py`, `test_phrases.py` |
| Expiry series tag | d7 | `classification.py::tag_expiry` | before floor unknown | implemented | `test_classification.py` |
| Golden #1 (bought ATM call/put, dropped entry, cover that never traded) | d12 | `test_golden_spy_2025_06_10.py` | manifest validated by `tests/fixtures/test_golden_manifest.py` (10 pass) | implemented; its `regenerate.py` end to end unverified (D) | — |
| Golden #2 (sold call spread, sold-leg stop, exit) | d12 amend (#3110) | `test_golden_spy_2026_10_09_sold_spread.py` | offline regenerate proven (`:781`) | implemented | — |
| Stops, momentum, re-entry, half day, estimated fees on real vendor data | ADR d12 "not proven" | — | — | unverified (by design) | ADR 0078 d12 + attribution list the gap |
| "No broker client/custody" import boundary | d1 | — | no test enforces it (ADR says so) | unverified | `git grep "from app.options_backtest" -- app` finds no importer outside the feature (clean today) |

### B. Cut candidates (production-dead, test-only callers; low value)

**B1. `app/options_backtest/results.py::SessionOutcome.exit_counts` (L313–316) and `::event_counts` (L318–321) + `from collections import Counter` (L10).** `git grep -n "exit_counts"`: def + 3 asserts (`test_replay_stops.py:185,376`, `test_replay_selling_stops.py:231`); `git grep -n "event_counts"`: def + asserts in `test_golden_spy_2026_10_09_sold_spread.py` (2), `test_needs_property.py:299`, `test_replay_selling_stops.py` (6), `test_replay_stops.py` (3). Nothing in `app/`, Frontend, Backend, contracts. Tests can use `kinds(outcome).count(...)` from `tests/options_backtest/_market.py` or `Counter(t.exit_reason …)`. Simulated cut with throwaway pytest plugin `plug/cutplugin.py`: only those call sites fail. Confirmed test-only.

**B2. `statistics.py::Ratio.exact` (L86–92) + `Inexact` import (L40).** No `app/` caller; wire serves numerator, denominator, `rounded_to_6_places` (`app/schemas/options_backtest_runs.py:211-241`). Test callers: `test_drawdown.py:77,104,158`; `test_statistics.py:162,174,210,245,825,842,894,909`. Simulated: the 11 `.exact()` assertions fail (parametrized `test_a_ratio_is_exact_where_it_ends…` on every row), nothing else. Cut: delete `exact` column of that parametrization + the `.exact()` asserts (each restates `Ratio(...)` equality or `rounded_to_6_places`, which stay). Confirmed.

### C. Not cut

| candidate | why |
|---|---|
| `picking.py::pick_contract` (L263–274) | test-only wrapper: 0 `app/` callers (`session_market.py:116`, `needs.py:76` use `prepare_leg`); 40 test refs (`test_picking.py` 27, `test_picking_prepared.py` 9, `test_needs_property.py` 2, `test_past_chain.py` 2); inside ADR 0068 parity-copy picker; `docs/architecture/engine-authority-map.md:139` names it; owner call |
| `cover_book.py::is_covered` (L290), `Held.empty` (L183), `Held.opened` (L192), `Admission.taken` (L214) | no production reader (replay reads only `.refused`); used by `test_cover_book.py` and Sacred `test_cover_book_parity.py` (`:194,212,260,396-410,428`). Keep (Sacred) |
| `RunSummary.exits_by_reason`, `gross_profit_usd`, `gross_loss_usd`, `Drawdown.measured_at`, `CashLevel.multiple`, `PastChainExpiryTag.version` | `git grep -w` only in `broker.types.ts`, fixtures/specs, `run_json.py` (`:107,336,339-340`), schemas, openapi; needs serial regen; gross profit/loss are owner definitions in #3030 (`CONTEXT.md:2012`); `version` is design intent (ADR d7) but nothing saves a tag. Owner ruling |
| `MinuteBar.high`/`.low` | carried and validated (`session_market.py:73`) but never read by a rule by design ("a high or low never triggers"); tests use them. Keep |

### D. Findings (no P0/P1)

| path::symbol | lens | sev | evidence | disposition |
|---|---|---|---|---|
| `replay.py:575-576` comment in `_legs_at_their_stop` | 7 | hygiene (misleading comment on a money-path gate) | says `close is None` "always holds here today"; it does not: a bought leg whose sale `may_leave` holds back stays HELD with a bar. `guard2.py` removes the guard: a waiting momentum leg's `momentum_hit` moves from minute 15 to 21 (`_stops` returns True every minute and blocks `_enter`). Someone trusting the comment could delete a load-bearing guard | fix: reword |
| `CONTEXT.md:1964` | 7 | hygiene | "…not counted in the summary yet: #3109"; `statistics.py:231,421` and `test_statistics.py::test_each_refusal_of_a_sale_or_its_cover_is_its_own_count…` count them; ADR 0078 d24 and `engine-authority-map` agree | fix: delete the clause |
| `test_replay_selling_invariants.py:138-139,304-305,316-317`; `test_capital_evidence_path.py:61-62` | 3/6 | robustness | `_a_scene` returns `None` ("not reached by construction") and tests `pytest.skip`; `none_scenes.py`: 0 of 150 seeds reach it; if generator drifts the family silently skips | fix: raise instead of `return None`/skip |
| slow marks: `test_replay_selling_invariants.py:376,505`, `test_needs_property.py:361,366` + `RULE_SAMPLE`, `_the_sample_every_pr_and_the_rest_daily` | 6 | hygiene | slow subset 447 tests in 4.9 s, PR subset 362 in 5.6 s; hand-picked seeds 1, 96, 161 save ~5 s local per PR; CI hardware not measured | owner-ruling; if dropped edit `engine-authority-map.md:137` |
| `statistics.py:415` | 3 | robustness (unreachable today) | `PICK_REFUSED` events with no reason silently skipped (`and event.reason`) while `_refusal_of` (L240) raises for entry refusals | fix: raise like `_refusal_of` |
| `test_replay_selling.py:250-262` | 6 | hygiene (kind 2/3) | pins sold-leg sentences in a replay test; `test_describe.py::test_a_sold_leg_says_it_sells_to_open…`, `test_a_sold_legs_momentum_gates_the_sale` own that wording | delete |
| `test_replay_selling_stops.py:757-774` | 6 | hygiene (kind 4) | counts `leaving_plan` calls `== 0`, no observable; `test_a_stop_on_the_sold_leg_alone…` (`:82`) asserts the observable | delete |
| `test_replay_selling_stops.py:800-829` | 6 | hygiene (kind 3) | third pin of same `square_off(sells=True)` sentence (`test_phrases.py:117`, `test_describe.py:592`); replay half repeats `…stop_on_the_protection…`, `…re_enters…closed_with_its_protection` scenes | trim or delete |
| `test_describe.py:507-526`, `:592-607` | 6 | hygiene (kind 3) | re-pin `phrases.reentry`/`phrases.square_off` templates already pinned in `test_phrases.py:94,117` | keep one wording pin (phrases), loosen describe's |
| `test_describe.py:430-440` | 6 | hygiene (kind 2) | `len(plain.window)==7`, `len(cutoff.window)==9`, `len(plain.costs)==3` break on any added sentence | delete or loosen |
| `test_phrases.py:75-77` | 6 | hygiene (kind 1) | `bought_or_sold("buy")=="bought"`, 2-entry dict lookup | delete |
| `test_capital_evidence.py:137-157` | 6 | hygiene (kind 3) | same title and numbers as `test_run_capital.py:287`, whose full-`CapitalEvidence` assert is a superset | delete |
| `test_replay_selling.py:561-571` | 6 | note | monkeypatches `admit_opening` to raise and asserts propagation: mock theater but the only test of "never caught here" | leave (money path) |
| `test_golden_spy_2025_06_10.py:815-886` | 1/3 | robustness | golden #1's `regenerate.py` "has not been run in this form" (own docstring + attribution); only folder guards tested; golden #2 has an offline end-to-end script test (`:781`) | owner-ruling (new work) |
| `fees.py:25-27` | 7 | hygiene | `import app.options_backtest.fees` (and `run`, `service`, `data_check`) fails without `POLYGON_API_KEY` via `app.broker.alpaca.__init__` building settings; documented; `replay.py` imports clean | leave |
| `tests/options_backtest/test_golden_*` | 6/7 | hygiene | 9 same-named stage tests differ only in numbers across the two golden files | leave (each proves its own bytes) |

### E. Skipped / gated

None skipped, none DB-gated. `pytest.skip` branches that cannot fire: the 2 sites in D3 (0 of 150 seeds). Slow-marked and deselected in PRs but verified passing: 447 tests across `test_replay_selling_invariants.py`, `test_needs_property.py`, `test_capital_evidence_path.py`. Parity tests really collect and run: `test_picking_parity.py` 218, `test_cover_book_parity.py` 8, `test_drawdown.py` engine parity, `test_account_picture.py:476`; vacuity guards exist (`_assert_the_comparison_had_something_to_compare`, `covered > 1000`, `uncovered > 1000`). Lint-as-test that stays: `test_needs.py::test_a_new_way_to_close_a_leg_must_be_weighed…`, `test_replay_selling.py::test_every_way_an_entry_is_refused_has_an_event…`.

### F. Not read

Read in full: all 17 sources, ADR 0078 + amendments, `CONTEXT.md` § Options Backtest, engine-authority-map rows, `test_replay_events`, `test_replay_event_numbers`, `test_phrases`, `test_describe`, `test_words`, `test_a_selling_run_in_every_view`, `test_capital_evidence_path`, `test_picking_parity`, helpers `_a_run`, `_a_selling_run`, `_big_run`, `_market`, `_results`, `_words`, `_golden_vendor`, `setup_fixtures`, both `attribution.md`, `regenerate.py` heads. Part: `test_replay_money` (L180–370), `test_replay_selling` (L225–265, 553–590, 640–870), `test_replay_selling_stops` (L78–116, 740–829), `test_run_capital` (L1–200, 280–320, 436–600), `test_capital_evidence` (L125–182), `test_run_slippage` (L170–210), `test_statistics` (L880–1011), `test_drawdown` (L60–160), `test_setup` (L440–575), `test_stored_setup_reasons` (L1–120), `test_picking_prepared` (L1–40, 140–180), `test_cover_book_parity` (L1–200), both golden tests (heads/tails), `test_replay_selling_invariants` (L1–200, 295–320). Skimmed: `test_replay_entries`, `test_replay_reentry`, `test_replay_stops`, `test_cover_book.py`, `test_classification.py`, `test_fills.py`, `test_fees.py`, `test_needs_property.py`, `test_run_cash.py`, `test_run_missing_data.py`, `test_run_wire.py`, `test_run_summary.py`. Not opened: golden fixtures' vendor JSON bodies and `expected.json` (hash-validated by `tests/fixtures/test_golden_manifest.py`, 10 passed). Frontend wire-field consumption checked by `git grep`, not Vitest.

## B2 — data side, run launch, `options_backtest.py` + `options_backtest_chain.py` routers — read at d1fe80ce

Headline: no P0/P1. Slice pytest: 724 passed, 49 skipped (all `POSTGRES_URL not configured`; the driver's Postgres run passed all of them). ruff lens set: 4 BLE001 (`fetching.py:600`, `:654`, `service.py:231`, `:255`), all deliberate catch-and-carry / catch-and-record; no S110/S112/E722/T201/DTZ*/F401/F841/ERA001. `python scripts/export_openapi_contract.py --check` exit 0 (snapshot == Pydantic for both routers). No `datetime.now()`, `pd.to_datetime`, `time(9, 30)`, `mcal`, fixed ET offset in the slice; every expiry through `option_expiry_anchor_ms`/`option_expiry_date`, sessions through `session_window_for_date`.

### A. Feature table

| feature | requirement | entry | reachable | verdicts | class | evidence |
|---|---|---|---|---|---|---|
| Session clock | #3030 pickers | `GET /session-clock` `routers/options_backtest.py:54` | yes (`main.py:1308`, `options-backtest.service.ts:84`) | n/a | implemented | `test_options_backtest_endpoints.py:65` |
| Describe setup | #3030, ADR 0078 | `POST /setups/describe` | yes (service.ts:94) | 422 paths tested | implemented | endpoints tests :76 :108 :123 :208 :226 :254 :279 |
| Limits | #3030 | `GET /limits` | yes (service.ts:123) | each served limit proven the enforced one | implemented; 2 of 9 fields unread by page (C2) | endpoints :504 :536 :548 |
| Saved setups list/save/delete | ADR 0078 d.23 | `GET/PUT/DELETE /setups…` | yes (service.ts:102/176) | 422 + name rules run; list/save/delete need DB | implemented, DB half ran only in driver's PG run | 14 DB-gated tests |
| Past chain day | #3030, ADR 0078 | `GET /past-chain/day` | yes | not stored / unreadable / refusal / 503 | implemented | `test_options_backtest_chain_endpoints.py:125–294` |
| Past chain expiry | same | `GET /past-chain/expiry` | yes | + window cap, instant refusals | implemented | chain endpoints :321–628; `test_past_chain.py` |
| Past chain download | ADR 0078 d.16, d.23 | `POST /past-chain/download`, control secret | yes | vendor/lake failure is an item; refusals before any request; cancel held | implemented | chain endpoints :880–1280; `test_chain_download.py` |
| Check data | ADR 0078 d.17–18 | `data_check.check_data`, `lake_check.LakeCheckData`; route in `options_backtest_runs.py` | yes | every verdict + stored-day table tested | implemented; real-catalog SQL twins DB-gated | `test_data_check.py` 45, `test_unreadable_days.py` 77; 32 DB twins in `test_lake_check.py` |
| Download phase | ADR 0078 d.15–16, d.21 | `fetching.ensure_run_data` from `service._work` | yes | chunks, stop, grace, wait tested | implemented | `test_fetching.py`, `test_fetching_liveness.py` |
| Lake reader | ADR 0078 d.17 | `lake_data.LakeRunData` | yes | None / empty / fault apart | implemented; `lake_root` option has no production caller (B2) | `test_lake_data.py` |
| Run launch + failure words | ADR 0078 d.18–21 | `service.sessions_of_range`, `execute_run`, `failure_of` | yes | cancel, fail, save-timeout, end-not-recorded | implemented; DB store half DB-gated | `test_service.py`; 3 DB-gated |
| Jobs wiring | ADR 0078 d.21 | `phases.py:147`, `JobsApi.cs:51`, `JobsApiTests.cs:66` | yes | all 5 phase ids emitted by `service.py:72–76` | implemented | `test_service.py:286`, `test_options_backtest_runs_endpoints.py:586/663` |

### B. Cut candidates

**B1. `data_check.py` `SessionCheck.trading_date`** (property lines 194–197) — confirmed. Delete only the property; `et_date_at_ms` stays imported (used by `TestableStretch.first/last`, `check_data`). Evidence: AST scan of every `.trading_date` receiver in `app/` and `tests/` for the options-backtest files finds no `SessionCheck` receiver; `git grep -n "trading_date" tests/options_backtest/test_data_check.py` has only parameter names; docstring says "Derived, never stored or sent". Orphans nothing; `test_data_check.py` green.

**B2. `lake_data.py` `LakeRunData.__init__(lake_root=…)` / `self._lake_root`** — confirmed test-only inside the slice, probable overall. Delete kwarg + attribute (lines 215–226, docstring 218–220) and pass-throughs `lake_data.py:241`, `:274`, `:297`, `lake_check.py:99` (`option_read_root(self._lake_root)` → `option_read_root()`); delete tests `test_lake_data.py:900–926` (`…alternate_mount…`, `…not_a_raw_lake_root_is_refused`), then `import shutil` at line 35. Evidence: `git grep -nE "(LakeRunData|LakeCheckData)\("` shows `lake_root=` only at `test_lake_data.py:913`, `:926`; in first commit `4983d282`, never wired. **Orphans outside the slice (data_lake's):** `lake_root` param of `option_bars.read_option_day_bars` (~255), `option_listing.read_option_listing` (~391), the non-None branch of `option_lake.option_read_root` (477–482), `tests/unit/data_lake/test_option_bars.py:1706,1725`, `test_option_listing.py:1361,1380`. Their only production caller would be gone.

**B3. `fetching.py` `ensure_run_data(…, data: LakeRunData | None = None)`** (line 212; `reader=data or LakeRunData(...)` line 226) — confirmed. Delete the parameter; keep `_Download.__init__(reader=)`, pass `LakeRunData(setup.underlying)`. Evidence: `git grep -n ensure_run_data` → only app caller `service.py:292` (no `data=`); test helpers `_fetch`, `_run` forward only `on_log`, `pace`, `should_stop`. Orphans nothing.

**B4. `fetching.py` `DownloadReport.sessions_prepared`, `_Counts.sessions`** (lines 157, 166, 172–175, 307) — probable, low value. `git grep -nw sessions_prepared` → only `fetching.py:157` + 10 test asserts (`test_fetching.py:101,153,491,541,586,603,622`; `test_fetching_liveness.py:318,798`); `service._work` logs only the four listing/day counts. Tests that read it are edited, not deleted.

### C. Not cut

- C1 check-data fields nobody reads: `first_testable_session_open_ms`, `last_testable_session_open_ms`, per-session `session_close_ms`. Producer: `schemas/options_backtest_runs.py:186–187`, `:201–202`; `data_check.py::DataCheck.first_testable/last_testable` (232–238), `SessionCheck.session_close_ms`. Page reads `longest_testable_stretch` only; `git grep -nw first_testable_session_open_ms Frontend` → `broker.types.ts:22983`, `testing/fixtures.ts:163`, `options-backtest-page.check.spec.ts:438` (filler, unasserted). Waits on serial OpenAPI + `broker.types.ts` regen; edits to `test_data_check.py:447–452`, `:494`, `test_options_backtest_runs_endpoints.py:811–812`, `:847`.
- C2 `OptionsBacktestLimitsResponse.max_reentries_per_leg`, `.max_range_sessions` (`schemas/options_backtest.py:99–103`, router `:74`, `:77`): Frontend never reads them (`service.ts:122` "The page copies none of it"; re-entry compared only to `Number.MAX_SAFE_INTEGER` `setup-build.ts:73`). Owner ruling (wire page or drop). If dropped: router stops importing `MAX_RANGE_SESSIONS`, so `limits.py` and `test_the_setup_routes_read_the_range_limit_without_importing_the_run_service` (endpoints :519) go too; needs regen.
- C3 past-chain wire fields no page code reads: `PastChainExpiryTag.version` (`schemas/options_backtest_chain.py:53`), `PastChainContract.steps_from_the_money` (:163; Frontend only `past-chain-view.spec.ts:478`, `testing/fixtures.ts:96`; Python `test_past_chain.py:545…`), `PastChainSide.at_the_money_strike` (:192). Cutting steps would remove the 2nd tuple element of `_Window.shown` and `place` ladder index in `past_chain.py:436–451`. Owner ruling; `version` may be an intentional receipt; needs regen.
- C4 `now_ms` params of `ensure_chain_data`/`fetch_refusal` passed only by tests — injected-clock seam; left.

### D. Findings (no P0/P1; traced, not reproduced)

| where | lens | sev | evidence | disposition |
|---|---|---|---|---|
| `schemas/options_backtest_chain.py:263`, router `:176` | 5 | robustness | `contracts: list[str]` no `max_length`; every symbol parsed on the event loop (`contract_from_symbol`) before the 60-cap in `ensure_chain_data`; control-secret gated | fix: `max_length` |
| `fetching.py:141`, `service.py:239` | 3 | P2 diagnostics | `DownloadFailed.detail` ("kept for a developer") read by nothing, logged nowhere (`execute_run` raises `from None`; job route logs only the code `options_backtest_runs.py:252`); survives only in catalog `ErrorMessage` (`catalog_client.py:1226`) | leave-with-reason or log once |
| `options_backtest_runs.py:104`, `data_check.py:329` | 5 | robustness | HTTP check calls `check_data` with default `_never_stop`, no `on_session`, in `asyncio.to_thread`; a 1,300-session range cannot be cancelled; a hung catalog read (`lake_check.py:117`, 60 s) ties a default-executor thread; Frontend disables a second click | owner/runs-slice decision |
| `chain_download.py:185–196` vs `fetching.py:388–395` | 3 | P2 (traced) | underlying capture reports `fetched` without reading the day back (the run raises `NOT_IN_THE_LAKE` there; the listing capture here reads back); a stored file with no regular-session bar reads `None` (`lake_data.py:256–257`) and would be reported `fetched` while the chain shows `not_stored` | fix: read back, as the listing does |
| `service.py:440–444`, `lake_check.py:117` | 3 | P2 wording | `CallerStoppedWaitingError` from the 60 s catalog read during **check** becomes `DOWNLOAD_FAILED` "What it fetched is kept", when nothing was fetched | leave-with-reason |
| `schemas/options_backtest_chain.py:27` vs `setup.py:49`, test `chain_endpoints.py:1369` | 7 | hygiene | `^[A-Z]{1,5}$` literal stated twice, test pins them equal | fix: import one; test goes |
| `chain_download.py:79,195` vs `fetching.py:123` | 7 | hygiene | two codes for "stock pipeline could not fetch underlying": `underlying_not_fetched` (not in `LAKE_REASON_WORDS`) vs `underlying_bars_not_materialized` | leave-with-reason |
| `past_chain.py:339` | 7 | hygiene | `ChainExpiry.expiry` never read; `from_expiry(chain, expiry_ms)` re-takes ms | leave |
| `ensure_chain_data` route | 5 | robustness | no overall deadline; bounded by lake timeouts (30 s/request, up to 630 s stock row); ADR 0078 d.16 documents | leave-with-reason |
| ADR 0078 "What this does not decide" | 5 | pointer | run job route `POST /api/jobs-internal/options-backtest` not under control secret though its download spends vendor requests | owner ruling, already recorded |

Test bar (lens 6):
- `test_data.py:12` `test_a_value_is_captured_with_the_sha256_of_its_file` trivial (dataclass stores its field) — delete
- `test_lake_data_stored_listing.py:40`, `:44` duplicate `test_lake_data.py:597`, `:849` (`listing()` is a thin call over `stored_listing()`, same None + worker-thread guard)
- `test_past_chain.py:868` `test_the_cap_of_a_window_is_the_number_a_download_takes…` pure constant arithmetic; survivor `:874` exercises real cap
- `test_fetching.py:159` `…at_most_four_contract_requests_in_flight…` subsumed by `test_fetching_liveness.py:101` (peak == 4); `test_chain_download.py:867` `…four_are` subsumed by `:884`
- `test_options_backtest_chain_endpoints.py:1322` `test_the_three_routes_are_served_under_the_setup_routes_prefix` route-config pin, covered by functional tests + snapshot gate
- `test_options_backtest_endpoints.py:519` subprocess import-graph lint stays while `limits.py` exists; goes with C2

### E. Skipped/gated (49, all `POSTGRES_URL not configured`)

`test_lake_check.py:163` (1), `:371` (30 parametrized), `:471` (1): real `DataLakeArtifacts` SQL twins; need the .NET-migration-created table. `test_service.py:1130`, `:1154`, `:1181`: `DbRunStore` round trips, no fake-store twin. `test_options_backtest_endpoints.py`: 14 tests (spelled money ×4, list, save-at-clock, replace, stale row, odd rows, examples ×3, delete, URL survival); `test_a_setups_money_is_served_in_plain_digits…` (:191) sits under the "No database" heading but is DB-gated. Nothing that cannot fire; every parametrized test in `test_unreadable_days.py` collects (77).

### F. Not read in full

Skimmed: `test_fetching_liveness.py` (870 l), `test_chain_download.py` (1,116), `test_past_chain.py` (989), `test_unreadable_days.py` (587), `test_options_backtest_chain_endpoints.py` (1,375; header + contract section), `test_lake_data.py` (first 150, last 100). Helpers `_run_data.py`, `_fake_connection.py`, `_headers.py`, `_refused_days.py` skimmed. `Backend/Jobs/JobsApi.cs` registry row + its test only. `options_backtest_runs.py` + its schema read for launch + check route only. No JS/TS tests run; no Postgres.

## B3 — stored runs, saved setups, run detail, the two run routers — read at d1fe80ce

Headline: no P0/P1. #3085 liveness fix is in every reachable path; OpenAPI snapshot current (`scripts/export_openapi_contract.py --check` exit 0). Reader runs (no PG): 18 unit files 437 passed / 97 skipped; 9 router files 241 passed / 63 skipped; ruff lens set clean; `test_picking_parity.py`, `test_sessions.py`, `test_account_picture.py` 265 passed. (Driver's PG run: all 3,652 passed.) Paths: `P/` = `PythonDataService/`, `FE/` = `Frontend/src/app/components/options-backtest/`.

### A. Feature table

| feature | requirement | entry | reachable | verdicts | class | evidence |
|---|---|---|---|---|---|---|
| Check data | #3030, ADR 0078 d16–21 | `routers/options_backtest_runs.py::check_the_data` | yes (`main.py:1356-1359`) | ok / no testable session / 400 range + 503 catalog | implemented | `test_options_backtest_runs_endpoints.py:787,825,850,863,890,906,921` |
| Start run job | #3030 | `::start_options_backtest_job`, `Backend/Jobs/JobsApi.cs:51` | yes | 202 + worker; 400; 409 dup; 422; worker not started → failed; cancel; failed; unexpected | implemented, SQL half unverified (store faked by `Harness`) | tests `:191,351,440,539,551,571,606,621,672,738` |
| History `GET /runs` | #3030, #3085 | `::list_the_runs` | yes, `FE history-tab.component.ts:59` | liveness off-loop one hop (`:973`); SQL path DB-gated | implemented / SQL DB-gated | `:973`; `:1237,1265` DB |
| Open run `GET /runs/{id}` | #3030, #3085 | `::get_the_run` | yes, `run-panel.component.ts:133` | wire shape non-DB (`test_run_wire.py`); damaged summary → bare 500 | partial | `repro_open_run_500.py` (D1) |
| Delete `DELETE /runs/{id}` | #3030, #3085 | `::delete_the_run` → `delete_unless_alive` | yes, `history-tab.component.ts:188` | 409 running; 204 job gone; 404 | implemented | `:1130,1149` (fake); `:1408,1421` DB |
| Trades page | #3030 | `run_detail.py router::list_the_trades` | yes, `run-trades.component.ts:46` | 404/409/422; paged; leg filter with lost setup | implemented (route mapping DB-gated) | liveness 409/404 + hostile 422 sweeps; `endpoints.py:240-311` DB |
| One session timeline | #3030, #3086 | `::get_the_session` | yes, `session-view.component.ts:65` | series reasons; coded 500 on inconsistent session | unverified (route half DB-gated); lower layers verified | `test_session_series|levels|view.py`; `session_endpoints.py` 25 DB |
| CSV export | #3030, #3109 | `::export_the_trades` | yes, `run-export.component.ts:45` | whole file, spooled; error never half file; spool closed | implemented | `export_spool.py` ×5 (fake); `test_trade_export*.py` |
| Compare two runs | #3030 | `::compare_two_runs` | yes, `compare-view.component.ts:39` | 404 a/b; 409 not complete; 409 lost setup; 409 underlyings | implemented, mapping DB-gated | `compare_in_thread.py` ×2, liveness ×3; `compare_endpoints.py` 8 DB |
| Stored-run write/read, DDL v20 | #3030, ADR 0078 | `saved_runs.py`, `run_json.py`, `schema.py:914-1040` | yes | exact round trip; one tx; refuses non-own result | DB-gated; verified in the driver's PG run | `test_run_json.py` ×8; `test_saved_runs.py` 58 DB |
| Saved setups, DDL v19 | #3030, #3109 | `saved_setups.py` | yes | examples, name rule, cannot-open reason | partial (12 DB) | name/reason/example tests |
| Run stamps | #3108 | `run_stamps.current_stamps` ← `run.py:392` | yes | None for pre-#3108 runs | implemented | `test_run_json.py:88` |
| Session clock | #3030 | `session_clock.full_session_clock` ← `routers/options_backtest.py:51` (off-loop) | yes | weekend/early close/DST/no session | implemented | `test_session_clock.py` ×5 |
| Calendar week facts | #3030 | `sessions.py` ← `picking.py`, `classification.py` | yes | 4 years vs calendar; parity with resolver | implemented | `test_sessions.py`, `test_picking_parity.py` |
| Day market, window-only market | #3030 | `session_market.py` ← `run.py:269,303` | yes | malformed bars, unloaded contract, half day | implemented | `test_session_market.py` ×18 |

`account_picture.py` reference to `options_backtest.cover_book.assignment_obligation` accurate (`cover_book.py:309`; `test_account_picture.py:476` holds the two figures equal). Redis liveness paths: `GET /runs` `present_runs` one `to_thread` hop (`saved_runs.py:461`); `GET /runs/{id}` `get_run(..., not_asked)` then `present_run` `to_thread` (`:518`); `DELETE` `delete_unless_alive` peeks, asks in a thread, then locks (`:698-700`); trades/session/csv/compare `read_closely` asks after txn released, in a thread (`run_detail.py:361`); no other `job_is_live` call site in the slice. Off-loop/no-connection/no-lock tests: `runs_endpoints.py:973,1112,1130,1149`; `run_detail_liveness.py` entirely (non-DB, passed).

### B. Cut candidates

**`P/app/options_backtest/saved_runs.py::list_runs` (470–475)** — `git grep -w list_runs` in the module → def + `tests/options_backtest/test_saved_runs.py:45,394,419,420,433,951,1018` (all DB-gated); router uses `read_run_rows` + `present_runs` (`options_backtest_runs.py:132-133`). Delete with it: the test import; repoint 6 calls to `present_runs(await read_run_rows(conn, ...), is_live=...)`. Orphans nothing. Survivors DB-gated — run `test_saved_runs.py` on PG. Confirmed test-only; low value. (F2 flagged the same.)

**`P/app/options_backtest/trade_export.py::trades_csv` (149–151)** — `git grep -w trades_csv` → no `app/` caller beyond docstring `:12`. Test-only: `test_trade_export.py:131,159,181,187,191,214,232`, `test_trade_export_batches.py:103`, `test_a_selling_run_in_every_view.py:113`, `test_run_detail_reads.py:232` (DB), `routers/..._export_endpoints.py:256` (DB). Export route uses `header_bytes` + `rows_bytes`. Delete with it: docstring sentence `:12-14`; the `trades_csv` name in `docs/architecture/engine-authority-map.md:144`; repoint ~12 test lines to `header_bytes() + rows_bytes(...)`. Confirmed test-only; owner may prefer moving it to a test helper. Low value.

### C. Not cut

1. Wire fields no Frontend reader touches (scan of both schema files vs non-spec `Frontend/src`, then `git grep -w`). Every cut needs: schema edit → `python scripts/export_openapi_contract.py` → `cd Frontend/tools/openapi-codegen && npm run generate` → delete fixture keys (`FE testing/run-fixtures.ts:171-175,183,192,237-238,257-258`; `testing/fixtures.ts:163-164`; `run-detail/testing/detail-fixtures.ts:44,69,93`; `selling-fixtures.ts:33,61`; `run/run-result.exact-dollars.spec.ts:27`; `options-backtest-page.check.spec.ts:438`). `exits_by_reason`/`gross_*_usd`/`measured_at` also live in `statistics.py`/`run_json.py` (money-trail stats, Sacred-adjacent). Fields: `OptionsBacktestDataCheckResponse.first_testable_session_open_ms`, `last_testable_session_open_ms` (`schemas/options_backtest_runs.py:186-187,201-202`; also `DataCheck.first_testable/last_testable` `data_check.py:233-238`, `test_data_check.py:450-452,494`); `OptionsBacktestDrawdownResponse.measured_at` (`:254,265`, single-value Literal; row `test_run_wire.py:119`); `OptionsBacktestRunFiguresResponse.exits_by_reason`, `gross_profit_usd`, `gross_loss_usd` (`:483,486,488,545-552`); `OptionsBacktestFillsBannerResponse.fee_policy_kind`, `fee_per_contract_usd` (`:621-622`); `OptionsBacktestRunDetailResponse.calendar_identity`, `calendar_package_version` (`:722-723`; NOT NULL columns `schema.py:929-930` need a DDL decision; provenance stamps nobody compares: owner ruling); `OptionsBacktestTradeResponse.exit_group` (`schemas/options_backtest_run_detail.py:167`; CSV still carries it; column NOT NULL).
2. `saved_runs.py::read_result` (523–555): test-only (13 call sites in `test_saved_runs.py`, `test_service.py:1147,1178`); it is the exact-round-trip oracle `run_json.py:8-9` names under "Validated against"; `engine-authority-map.md` row 142 lists it as canonical; reads a P&L store. Not cut; at most move under `tests/` (owner).
3. `comparison.py::_setup_of` raise `RunSetupCannotBeRead` (`:315-318`): unreachable from route because `judge_pair` refuses first; fails closed; tested `test_comparison.py:528`. Left.
4. `OptionsBacktestJobResult` fields `sessions_tested`, `trades`, `net_profit_usd` (`schemas/options_backtest_runs.py:820-827`): the job's `job.completed` payload; not in OpenAPI; `fetchResult` never called for this type; runner needs a returned value. Left.

### D. Findings (no P0/P1; no silent coercions on read beyond four documented optional summary keys; all times int64 ms; no float/utcnow/hardcoded session time)

| path::symbol | lens | sev | evidence | disposition |
|---|---|---|---|---|
| `saved_runs.py::get_run` → `run_json.summary_from_json` | 3/5 | robustness | a complete run whose `summary_json` lacks a key answers `GET /runs/{id}` as bare 500 `{"error":"'sessions_traded'"}` (KeyError leaked; `repro_open_run_500.py`); session route answers a coded 500 and the rest of the run still opens (`session_endpoints.py:566`); nothing writes such a shape today; loud failure | leave-with-reason, or map `KeyError`/`ValueError` to a coded refusal |
| `saved_runs.py::DbRunStore.fail`/`cancel` (907–915) | 3 | robustness | discard `fail_run`/`cancel_run`'s `False` ("was not running", `:403-432`); `_record_the_end` (`service.py:249`) logs only exceptions; write guarded by `status='running'`; record is truth | leave-with-reason (optionally log the `False`) |
| `saved_runs.py::liveness_of`/`present_runs` (451–467) | 5 | robustness (traced) | liveness asked once per stored-running row, sequentially, each up to `JOB_STORE_TIMEOUT_SECONDS`=5 (`jobs/progress.py:61`); rows stay `running` forever (`:454`); hung Redis + long history (`limit` ≤ 1000) holds a default-executor thread N×5 s; history polls only while a run goes (`history-tab.component.ts:214`) | leave unless a Redis hang is seen; fix = one pipeline |
| route refusal codes (`SETUP_CANNOT_BE_READ`, `RUNS_OF_DIFFERENT_UNDERLYINGS`, `SESSION_NOT_IN_RUN`, `LEG_NOT_IN_RUN`, `STORED_SESSION_INCONSISTENT`) | 3/6 | robustness | every assertion of these codes is in a DB-gated test (`compare_endpoints.py:192,264`, `hostile.py:202,254`, `session_endpoints.py:587-684,736`, `endpoints.py:311,356,358`), so PR CI never fires them; Frontend shows the message and does not branch on them | fix: port a few to `_fake_connection.py` |
| `endpoints.py:361-384` `test_a_request_that_cannot_be_one_is_a_422_before_any_run_is_looked_for` (12) | 6 | hygiene | strict subset of `hostile.py` sweeps (`_RUN_IDS`×{trades,trades.csv} `:69`, `_SESSIONS` `:86`, limit/offset `:97`, leg_id `:122`); survivor not DB-gated | fix (delete) |
| `export_endpoints.py:299-303` `test_a_run_id_that_cannot_be_one_is_a_422` (4) | 6 | hygiene | ⊂ `hostile.py:69-76` | fix (delete) |
| `session_endpoints.py:761-771` `test_a_request_that_cannot_be_one_is_a_422` (7) | 6 | hygiene | ⊂ `hostile.py:79-94` | fix (delete) |
| `compare_endpoints.py:269-281` `…422_about_the_query_…` (7) | 6 | hygiene | same `loc[0]=="query"` assertion as `hostile.py:148-173` (14 params contain all 7) | fix (delete) |
| `session_endpoints.py:247,270,290,341,359,376,402(×4)` (6 tests + 4-case sweep, DB-gated) | 6 | hygiene | duplicates of `test_session_series.py:208,231,253,272,559,574,588,609,624` (same real tmp lake and `_refused_days.REFUSED_DAYS`); unit twins stronger; router twins add only 200 + fixed `SERIES_WORDS` (covered by `test_run_detail_wire.py:326`); keep `:89,155,206,516,566-690` as route smoke | fix (delete; probable) |
| `session_endpoints.py:429,456,488` (DB-gated) | 6 | hygiene | skip/no-trade/window cases duplicated by `test_session_series.py:175,156,656`, `test_run_detail_wire.py:472`; they also assert stored cash/events | owner-ruling (lower confidence) |
| `endpoints.py:317`, `export_endpoints.py:281`, `session_endpoints.py:744` (DB-gated) | 6 | hygiene | 404/409 mapping repeated for 3 routes; `liveness.py:99,124,144` asserts exact message on all three, non-DB; `export_spool.py:110` repeats export 409; real-SQL header read covered by `test_run_detail_reads.py:170,251` | fix (delete; probable) |
| `trade_export.py:4` | 7 | hygiene | `Reference:` cites `.claude/rules/temporal-rigor.md` (AGENTS.md forbids rule-file citations) | fix: cite ADR 0022 |
| `trade_export.py:121` vs `schemas/options_backtest_run_detail.py:214` | 7 | hygiene | `exit_kind` classification written twice; parity pinned by `test_a_selling_run_in_every_view.py:97,112` | leave-with-reason |
| `test_trade_export_batches.py:269` | 6 | hygiene | `@pytest.mark.slow` on a 3.5 s test, so the only bounded-memory proof runs only in the daily | owner-ruling |
| `engine-authority-map.md:142,144` | 7 | hygiene | names `read_result`/`trades_csv`; row 144 omits `liveness`, `compare_in_thread`, `export_spool` router tests | fix together with B |
| `schemas/options_backtest_run_detail.py:496-500` | 5 | robustness | session `trade_number` is `enumerate(...)`, not the stored one (`saved_runs.py:869-870` drops it); true because `complete_run` numbers from 0 (`:285-289`) but DDL checks only `>= 0` | leave-with-reason |
| export / session routes | 5 | robustness | no cap on concurrent exports/session reads (each holds a pooled connection + lake reads up to 168 contracts); single-owner tool | leave |

Counts: four 422 tests = 30 cases, all non-DB (run in every PR); series twins ~10 cases; 404/409 row 3 tests.

### E. Skipped / gated (reader's no-PG run; all `POSTGRES_URL not configured`; all pass on PG)

Unit 97: `test_saved_runs.py` 58 (lines `164,181,214,230,286,367,375,413,426,445,464,480,497,521,536,553,564,574,588,592,604`, `711(×19)`, `815,857,881,904,940,959,1006`, `1053(×4)`, `1082,1099,1125,1142,1159,1186,1208`); `test_saved_setups.py` 12 (`305,325,355,367,382,412(×3),418(×2),428,444`); `test_run_detail_reads.py` 27 (every DB test, `76…402`). Routers 63: `runs_endpoints.py` 10; `run_detail_endpoints.py` 8; `compare_endpoints.py` 8; `export_endpoints.py` 7; `hostile_requests.py` 5; `session_endpoints.py` 25. `P/tests/research/persistence/test_schema.py` 12 skipped (DDL v19/v20 `:358-393`). Cannot fire in PR CI: the 160 gated tests; they run only in the daily.

### F. Not read in full

Read in full: all 14 app modules, both routers, both schemas, `schema.py:890-1060`, `service.py:135-300,355-420`; `run_detail_liveness`, `hostile`, `endpoints`, `export_endpoints`, `export_spool`, `compare_endpoints`, `compare_in_thread`, `session_endpoints`, `test_session_view`, head of `_fake_connection.py`, `_stored_runs.py`, `setup_fixtures.py`. Skimmed: `test_saved_runs.py` body, `test_saved_setups.py`, `test_run_detail_reads|decisions|rows|wire.py`, `test_comparison.py`, `test_session_levels|market|series|result.py`, `test_stored_setup_reasons.py`, `test_run_json.py`, `runs_endpoints.py` outside `:336-540`, `:940-1165`. Not read: `_detail_scene.py`, `_run_data.py`, `_a_run.py` bodies. `broker.types.ts` not diffed against the snapshot (relied on `--check` + greps). `lake_data.py`, `data_check.py` only catch lists + first/last-testable use.

## F1 — Frontend page root, `run/`, `controls/`, `testing/` — read at d1fe80ce

Headline: no P0/P1. 45 spec files (root, `run/`, `controls/`) in one `ng test`: 731 passed, 0 skipped, worktree root printed. eslint `--max-warnings 0` clean on root/run/controls/testing. `tsc --noUnusedLocals --noUnusedParameters`: no hit in slice (one outside: `shared/symbol-catalog/testing/fake-symbol-catalog.ts:70`). Clean: no `new Date(string)`/`Date.parse`/`DatePipe`/`Intl.*`/`toLocale*` (instants via `etMidnightMs`, `etIsoDate`, `app-timestamp-display`); no console/TODO/eslint-disable/@ts-ignore; comments cite only #3030/#3108/#3109; no `.subscribe()` in production (RecordPoller / SetupSummaryChecker.stop via DestroyRef).

### A. Feature table (all reachable: route `app.routes.ts:239`, menu `shell/app-menu.ts:97`, no flag; shell reads `stickyContent` at `app.component.ts:208`)

| feature | entry | verdicts | class | evidence |
|---|---|---|---|---|
| Build a setup into request body | page + `setup-build.ts::buildSetup` | no clock "Reading…"; clock failure error + retry | implemented | `setup-build.spec.ts`, `page.legs.spec.ts`, `page.clock.spec.ts` |
| Plain-words read-back `POST /setups/describe` (debounced, generation-guarded) | `SetupSummaryChecker` | idle/checking/ready/invalid/failed; 422 at fields or whole-setup list | implemented | `setup-summary-checker.spec.ts`, `page.summary.spec.ts` |
| Server limits `GET /limits` | `limitsRead` resource | unread limits leave server to judge, page says so + retry | implemented; 2 served fields unread (C) | `page.limits.spec.ts`, `leg-draft.spec.ts` |
| Saved setups list/open/save/delete | `library` resource | list error; refusal reasons; stale "Saved" cleared; open-after-save race guarded | implemented | `page.library.spec.ts` |
| Export/import setup file (1 MB cap, server judges) | `exportSetup`, `importFile` | non-JSON, too large, 422 each say why and change nothing; latest file wins | implemented | `page.files.spec.ts`, `setup-file.spec.ts` |
| Dates as two NY midnights, outside the setup | `duration.ts`, `range` | DST + Sunday; refused range shown at dates | implemented | `duration.spec.ts`, `page.range.spec.ts`, `page.duration.spec.ts` |
| Check data (read-only, stale-stamped) | `DataCheckFlow` | 400 range refusal at the dates; 503/other `failed`; stale after edit/started run/chain download | implemented | `data-check-flow.spec.ts`, `page.check.spec.ts` |
| Start a run as a job, find its record, one at a time | `RunLaunch` | 4xx "not started"; no clear answer "may have started"; record not found stays busy | implemented; real jobs boundary (`Backend/Jobs/JobsApi.cs:51`) not exercised, only fake-jobs + HttpTestingController | `run-launch.spec.ts`, `page.start.spec.ts`, `page.poll.spec.ts`, `service.runs.spec.ts` |
| Come back while a run is going | `launch.takeUp()` | history unreadable → "could not check", not "none running" | implemented | `page.start.spec.ts:364-424` |
| Run panel: progress, 3 s record poll, cancel, result/failed/cancelled/interrupted | `RunPanelComponent` | record is the truth; read failure says so and keeps looking | implemented | `run-panel.spec.ts`, `run-panel.poll.spec.ts` |
| Result view: banner, headline, drawdown, capital evidence, sessions, slippage tolerance, cash ladder, day/month tables | `RunResultComponent` | "not recorded" for pre-#3108/#3109 runs | implemented | `run-result.spec.ts`, `run-capital.spec.ts`, `run-tables.spec.ts`, `run-result.exact-dollars.spec.ts` |
| History "Use this setup" back into the form | `useRunSetup` | name+highlight cleared, dates kept | implemented; `clock === null` silent no-op (D2) | `page.history.spec.ts:87-163` |
| Shift+Enter start shortcut | host `document:keydown.shift.enter` | held key, IME, textarea, History tab refused | implemented | `page.start.spec.ts:208-288` |
| Sticky run rail (#3139–#3141) | `.rail` sticky, `stickyContent` route data | stacked <1100 px uses fixed bar + `bar-spacer` | implemented; layout unverified in a browser (jsdom) | `app.component.spec.ts:270` |

### B. Cut candidates

**`options-backtest.types.ts`**
- B1: `:15` `Refusal`, `:29` `RunHeadline` (confirmed). `git grep -w Refusal` / `-w RunHeadline` over `src` + `tests` → own lines only; backing schemas stay referenced from generated types. Optional: `:28` `RunStatus` used only by `run/run-panel.spec.ts:5,407` (could use `RunListItem['status']`; its comment goes with it).

**`setup-problems.ts`**
- B2: `:19` `FIELD.name` and `:74` branch `if (loc[0] === 'path' && loc[1] === 'name') return FIELD.name;` (confirmed) + spec row `setup-problems.spec.ts:19`. `FIELD.name` no reader (`git grep -wE "(FIELD|field)\.name"` → producer + spec row). `problemsFromDetails` has one production caller `setup-summary-checker.ts:90` (describe, which has no path param `routers/options_backtest.py:60`); a save-time 422 goes `SetupInvalidError` → `reasonOf` → "Not saved. …" and never reaches it. Added in `c4ed127b` (2026-10-12) for a design that no longer exists.

**Spec files (test bar)**
- B3: `controls/controls.spec.ts:45-49` "names its group for a screen reader" (confirmed; `:51`, `:63` already look the group up `getByRole('group', { name: 'Re-entry kind' })`; also a11y-class).
- B4a: `options-backtest-page.sides.spec.ts:28` `expect(screen.queryByText(/only buying is offered/i)).toBeNull()` + title tail "and no longer says that only buying is offered" (confirmed tombstone, kind 5); first two assertions stay.
- B4b: `options-backtest-page.sides.spec.ts:104-145`, three tests in "what the server refuses is shown in the server's words" (probable): sentences are produced by the test helper's own `coverRefusal` (`testing/render-page.ts:74-91`), not the server; whole-setup 422 in summary pinned by `page.summary.spec.ts:262-282`; "sends side sell" by `sides.spec.ts:40`. If they go, `coverRefusal` (`render-page.ts:74-91`, call `:71`) has no consumer; `files.spec.ts:172` needs only entry/exit rules of `wholeSetupRefusal`.
- B5: `options-backtest-page.phases.spec.ts` whole file, 39 lines (probable): asserts the five phases' `friendly` text; `run/run-panel.spec.ts:17-53` asserts same visible outcome via real panel; `run-words.spec.ts:18-20` pins label; only unique assertion (humanised name not shown) is a property of `services/jobs.service.ts:484` `friendly ?? humanisePhase(phase)`. If kept, move that `queryByText(made)` line into `run-panel.spec.ts`.
- B6: `run/run-result.spec.ts:252-262` two tests "survives the whole ladder", "nothing traded" (confirmed): same `toleranceSentence` branches pinned exactly in `run-words.spec.ts:92-108`; keep `:246` (render proof) and `:264` (ladder table).
- B7: `setup-build.spec.ts:22-52` helpers `exampleByHand`/`noReentryExampleByHand` + describe `:54-66` (probable duplicate): used only by those tests; survivors `page.legs.spec.ts:34-74` build the same two shipped examples through the real form and assert `describe` last called with `exampleBody()`/`noReentryExampleBody()` with same `not.toHaveProperty` checks — stronger.
- B8: `options-backtest-page.duration.spec.ts:8-26`: `beforeAll` `File.prototype.text` polyfill `:18-24` dead (spec never reads a `File`; only `files.spec.ts` does) — confirmed; local `readText` duplicates `run-detail/testing/blob-text.ts::textOf`; last assertion of first test `:37` `filter(field.type === 'date')` repeats repo-wide `shared/date-picker/raw-date-field.contract.spec.ts`.
- If specs are deleted: drop keys from `Frontend/ci-spec-durations.json` (no guard checks stale keys; none stale now).

### C. Not cut

| candidate | why |
|---|---|
| `OptionsBacktestLimitsResponse.max_reentries_per_leg`, `.max_range_sessions` (no Frontend reader outside `broker.types.ts`, `testing/fixtures.ts:98,101`) | server model + router + Python contract tests (`test_options_backtest_endpoints.py:496,511`, `test_options_backtest_runs_endpoints.py:380`) + serial OpenAPI regen; `setup-build.spec.ts:151` says page leaves re-entry range to server by design → owner call |
| a11y-class tests (D8) | owner ruling |
| `describeDebounceMs` input (`page.component.ts:97`) | set only by specs, but 300 ms debounce is real; test seam |
| Unneeded `export` keywords: `CheckAsked`, `ReasonGroup`, `MissingPart`, `OptionsBacktestTab`, `RefusalRow`, `entryRefusalWords`, `StoredTime`, `BuildResult`, `DEFAULT_SLIPPAGE`, `DEFAULT_PRICE_STEP`, `HeldAnchors`, `SetupFile`, `RenderedRun`, `CREATED_AT_MS`, `RUN_STAMPS`; spec-only `legFromBuilder`, `copyOfLeg`, `nextLegId`, `NOON_MINUTE_ET`, `MAX_SETUP_FILE_BYTES`, `cleanMessage`, `RunApi` | used inside own file; refactor, out of scope |
| `FakeService.describe` mirrored rules (`render-page.ts:54-91`) | entry/exit part still used (`files.spec.ts:172`); only `coverRefusal` follows B4b |

### D. Findings (no P0/P1)

| path::symbol | lens | sev | evidence | disposition |
|---|---|---|---|---|
| D1 `run/run-panel.component.ts:100-107,131` | 3 | P2 (traced) | on run id change `cancelling`/`cancelProblem`/`record` reset but `readFailure` not; a failed read of run A keeps "…could not be read. The page is still looking." under run B until B's first read; `run-panel.spec.ts:338-364` cover record not `readFailure` | fix: `readFailure.set(null)` in id-change effect |
| D2 `options-backtest-page.component.ts:339-342` `useRunSetup` | 3 | P2 (traced) | `if (clock === null) return;` silent no-op: History "Use this setup" does nothing and tab does not switch when the session clock was never read; saved-setups open (`[canOpen]`) and Import (`[disabled]`) gated, this not; no case in `page.clock.spec.ts` | fix (disable button or say why) or owner-ruling |
| D3 `options-backtest.service.ts` | 5 | robustness | no timeout on any `firstValueFrom(http…)`; hung `describe`/`checkData` leaves `summary`/`check` `checking` (`canCheck` false); repo-wide: no timeout interceptor `app.config.ts:63` | leave-with-reason |
| D4 `session-time.ts:20-23` `LEAST_MINUTES`, `testing/render-page.ts:54-91` | 5/6 | robustness | two hand copies of `setup.py::_the_window_can_hold_a_trade` (`:277`, "mirrored from setup.py"), no parity test naming `setup.py`; server-side rule change appears only as a missing field message; specs run green against the fake's copy | owner-ruling: one contract test, or leave |
| D5 `session-time.ts:5` `NOON_MINUTE_ET = 12*60` | 4 | hygiene | UI anchoring convention, not a session boundary; open/close from `GET /session-clock` | leave |
| D6 test-only duplicates | 7 | hygiene | `readText(blob)` ×3 (`page.readback.spec.ts:24`, `page.duration.spec.ts:9`, `page.files.spec.ts:12`) vs `run-detail/testing/blob-text.ts::textOf`; `openExample` in `readback.spec.ts:33`, `files.spec.ts:35` vs `testing/page-actions.ts:57`; `going()` `run-panel.spec.ts:14` vs `testing/run-script.ts::goingDetail`; `MS_PER_MINUTE` `testing/fixtures.ts:5` vs `session-time.ts:3`, `const MINUTE` `render-page.ts:47`; `startButton` ×5, `lastBody` ×2, `shiftEnter` ×2 | fix (mechanical, test-only) |
| D7 `.again` ×9, `.sr-only` ×10 in component SCSS | 7 | hygiene | copied per component | leave |
| D8 a11y-class tests | 6 | owner-ruling | AGENTS.md "Accessibility" 2026-10-12. `page.a11y.spec.ts`, `page.a11y-run.spec.ts` (axe ×5), `page.focus.spec.ts`, `page.summary.spec.ts:133-180` (4), `:412`, `:435`, `page.start.spec.ts:219`, `run-panel.spec.ts:105,152,168`, `run-failure.spec.ts:45,101`, `run-tables.spec.ts:99`, `run-result.day.spec.ts:54,74`. CI cost 25.9 s of the slice's 178.7 s (a11y-run 12.6, a11y 9.8, focus 3.5); three slowest local tests are axe runs (2.79, 2.78, 2.02 s); the #3123 near-5 s test is probably one of these. Production code serving them: `focusHeading()` ×3, `afterNextRender` focus moves, `RunFailureComponent.listId`/`instance` | owner-ruling |
| D9 copy pins | 6 | hygiene | `page.words.spec.ts:24-37` regexes on `EXPIRY_HINTS`, `:40-50` Instrument-card text, `sides.spec.ts:31-37` hint regex; absence checks `words.spec.ts:21,41-46` stay | leave / author's call |
| D10 `app.routes.spec.ts:145-151`, `shell/app-menu.spec.ts:34-40` | 6 | hygiene | route/menu pins ("what not to test") but `:150` is the only guard that the real route declares `stickyContent` | leave |
| D11 `testing/render-page.ts:98-131` `fakeService` | 6 | robustness | page specs run against a double re-implementing server validation | leave while D4 stands |
| D12 pointer: `Frontend/src/assets/docs/bot-business-manual.md:64`; `PythonDataService/app/schemas/traded_asset_acknowledgement.py:154` ("Options have no backtest here.") | 7 | hygiene (unverified) | deploy-acknowledgement copy that may predate this page; may still be true (about the deployed strategy's evidence) | owner-ruling |

Pairs judged different layers and left: `setup-summary-checker.spec.ts` vs `page.summary.spec.ts:87`; `data-check-flow.spec.ts` vs `page.check.spec.ts:451`; `run-launch.spec.ts` vs `page.start.spec.ts:290-363`; `leg-draft.spec.ts` vs `page.limits.spec.ts`.

### E. Skipped/gated

None skipped; nothing DB-gated; none that cannot fire. Summed local test time 137.9 s, longest single test 2.79 s, no per-test timeout configured (vitest default 5 s).

### F. Not read

Read in full: production TS/HTML/SCSS of root, `run/`, `controls/`; testing helpers (`fake-jobs`, `render-page`, `page-actions`, `render-history`, `render-run`, `run-script`, `fixtures`); specs `page.a11y*`, `focus`, `phases`, `words`, `clock`, `range`, `poll`, `limits`, `readback`, `history`, `controls.spec`, `setup-problems.spec`. Partially: `page.check.spec.ts` 1-185, `page.summary.spec.ts` 181-395, `page.sides.spec.ts` 1-150, `page.legs.spec.ts` 25-76, `page.duration.spec.ts` 1-100, `run/run-panel.spec.ts` 1-130 + 318-366, `run/run-result.spec.ts` 243-275, `setup-build.spec.ts` 1-66, `testing/run-fixtures.ts` 1-60. Titles only: `page.start/library/files`, `run-launch`, `data-check-flow`, `data-check-view`, `setup-summary-checker`, `run-name`, `read-back-scroll`, `setup-file`, `setup-limits`, `options-backtest.service*`, `session-time`, `leg-draft`, `duration`, `run/run-capital`, `run/run-failure`, `run/run-panel.poll`, `run/run-panel.detail`, `run/run-result.day`, `run/run-result.exact-dollars`, `run/run-tables`. Cut candidates not re-run (read-only): "survivors" evidence is the survivors' own assertions.

## F2 — Frontend `cards/` (36) + `history/` (10) — read at d1fe80ce

Headline: no P0/P1. Slice is tidy: no dead exported symbol, unbound input/output, never-set template branch or unused SCSS class. One lens-2 cut (one dead CSS rule). History specs 56 tests, none skipped. Page specs exercising cards all pass (legs 18, sides 13, limits 7, library 19, summary 27, check 28, duration 12, start 33, history 10, words 7; history-tab 27, compare 14, compare.poll 1, exact-dollars 3, paging 5, poll 6). `eslint cards history --max-warnings 0` clean; `tsc --noUnusedLocals --noUnusedParameters` clean for both configs.

### A. Feature table (all reachable: checked-in route, Backtest/History tabs, no flag)

| feature | requirement | entry | normal/empty/failure | class | evidence |
|---|---|---|---|---|---|
| Instrument card (raw-price symbol picker) | #3030 | `instrument-card` | pick sets underlying; empty "Pick an underlying."; server problem at field | implemented | `page.summary.spec`, `page.words.spec` |
| Duration card | #3030, ADR 0075 | `duration-card` | no dates at start; refusal shown; bounds from provider/Check data | implemented | `page.duration.spec` 12 |
| Entry card | #3030 | `entry-card` | out-of-session time is a field error, nothing sent | implemented | `page.summary.spec` "typed times" |
| Legs card: builder, leg cards, copy/delete, square off, rules | #3030, #3105, #3107 | `legs-card` | max legs full, out-of-range contract, rule off sends nothing | implemented | `page.legs.spec` 18, `page.sides.spec` 13, `page.limits.spec` 7 |
| Costs card (cash, slippage, fee kind, per-contract fee) | #3030 | `costs-card` | per-contract field disabled unless kind per_contract (`page.component.ts:125`) | **partial**: `price_step_usd` in draft and body but no field edits it (D3) | `setup-build.ts:55`, `setup-draft.ts:98`, `page.files.spec:118` |
| Bottom bar (name, Save, Check data, Start, Shift+Enter) | #3030 | `bottom-bar` | saving, refused, name-rule | implemented | `page.library.spec`, `page.start.spec` 33, `page.check.spec` |
| Saved setups menu | #3030 | `saved-setups-menu` | loading, error+retry, delete refusal | implemented | `page.library.spec` 19 |
| Plain-words summary / read-back | #3030 | `summary` + `setup-sentences` | idle, checking, ready, invalid, failed+retry | implemented | `page.summary.spec` 27 |
| Check data panel + sessions + Run available dates | #3030 | `data-check-panel` | stale, nothing-testable, failed+retry | implemented | `page.check.spec` 28 |
| Launch notice | #3030 | `launch-notice` | not_started, start_unanswered, record_unread, unchecked | implemented | `page.start.spec` |
| History list | #3030, #3093 | History tab | empty; error + Try again (list hidden on error); dates date-et | implemented | `history-tab.spec` L38–L85 |
| History open run / Use this setup | #3030 | row Open | no setup gives reason + disabled | implemented | `history-tab.spec` L211, `page.history.spec` L102, L132 |
| History delete | #3030, #3129 | row Delete | 409 reason shown, list re-read | implemented UI; server side DB-gated (E) | `history-tab.spec` L226–309 |
| History compare | #3030 | Compare | server refusal shown | implemented | `compare.spec` 14 |
| History poll while a run goes | #3030 | automatic | stops off-show/opened/comparing | implemented | `poll.spec`, `compare.poll.spec` |
| "Show older runs" capped at 1000 | #3093 | History | known accepted limit | implemented | `paging.spec` 5 |

Clean: lens 3 (failed read hides list + Try again `history-tab.component.ts:62`, `html:11`; failed delete sets `deleteProblem` and re-reads `ts:183–194`); lens 4 (open dates `date-et`, `created_at_ms` `local` `history-tab.component.html:33,38`; no `new Date`/`Date.parse`/`DatePipe`/`Intl`/string time; `toLocaleString` once on a count `ts:68`); lens 5 no `any`, `options-backtest.types.ts` derives from `broker.types.ts`; lens 7 no eslint-disable/@ts-ignore/console/TODO, no plan/PRD/rule-file comment. Every `choices.ts` value consumed; every `var(--…)` defined; `@container page-grid` declared by `shell/page-body.component.scss:4`.

### B. Cut candidate

**`cards/leg-builder-card.component.scss:3` — rule `.band h3:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }`.** Only `h3` it can match is `#ob-builder-title` (`leg-builder-card.component.html:3`), which has no `tabindex` and is never focused; `git log -S'tabindex="-1"' -- …/leg-builder-card.component.html` → no commit (never had one); component TS focuses only `addButton` and `contracts`. Arrived in `460d9e12` (layout slice). Orphans nothing. Confirmed. Caveat: a focus style → owner's accessibility ruling may put it out of scope; still a rule that cannot match.

Nothing else dead: every exported symbol has a non-test importer (`SaveStatus`, `LibraryStatus` imported by page; `RUNS_LISTED`/`RunsListed` by History); `focusHeading`/`focusDelete`/`focusAdd` have callers; every card input/output bound; every SCSS class has a template user.

### C. Not cut

| candidate | why |
|---|---|
| `PythonDataService/app/options_backtest/saved_runs.py:470` `list_runs` | test-only wrapper: `git grep -nw list_runs` → def + 7 call sites in `tests/options_backtest/test_saved_runs.py` (L394, 419, 420, 433, 951, 1018; import L45); route composes `read_run_rows` + `present_runs` itself (`routers/options_backtest_runs.py:132-133`). Not delete-only: those DB-gated tests must be rewritten to `present_runs(await read_run_rows(...))`. B3's file. Lead. |
| `finished_at_ms` on `OptionsBacktestRunListItemResponse` (`schemas/options_backtest_runs.py:685`, inherited by detail) | never read by non-test Frontend; needs regen + schema + test edits. Owner ruling (D2). |
| History focus machinery (`focusAfterList`, `focusAfterDelete`, `heading`/`compareButton` refs, `afterNextRender` focus) | a11y class, not a cut |
| `fee_policy_kind`, `fee_per_contract_usd` on banner schema; `calendar_identity`, `calendar_package_version` on run detail | no non-test non-generated Frontend reader (`git grep -nw`); F1/F4 files; pointer |

### D. Findings (no P0/P1)

| # | path::symbol | lens | sev | evidence | disposition |
|---|---|---|---|---|---|
| D1 | `schemas/options_backtest_runs.py:694` list/detail `fee_policy` not shown | 1,5 | P2 partial | schema comment L724–728 says fee policy in words "is on the run itself either way", fallback for a run whose setup cannot open (no banner). Frontend reads only `banner().fee_policy` (`run/run-banner.component.html:5`); no other reader. Python `banner.fee_policy == record.fee_policy_text` (`from_stored` L764). A History-opened run without a setup shows figures with no fee line. | owner-ruling: show run-level `fee_policy` when `banner` null (F1/F4 files) or drop the field |
| D2 | `finished_at_ms` list item | 5,7 | hygiene | served in list+detail, never read; fixtures only (`testing/run-fixtures.ts:31,256`) | owner-ruling |
| D3 | `costs-card.component.html` no price step field | 1 | partial, not defect | `priceStep` carried through draft (`setup-draft.ts:98,142`) and `buildSetup` (`:55`), import/export-only; a server problem lands in "other" (`setup-problems.spec.ts:51-54`); ADR 0078 L56 calls it a setting on the setup | owner-ruling (build or drop) |
| D4 | `history/runs-listed.ts:12` `{page: 200, most: 1000}` | 5 | robustness | hand-copied from route default and `le=1000` (`options_backtest_runs.py:121`); generated type drops the bound; server pinned by `test_a_history_limit_outside_1_to_1000_is_a_422`; frontend constant not tied to it | leave-with-reason |
| D5 | `history-tab.component.ts:212-216` poll | 5 | robustness | server down + a run reading running → re-reads every 3 s forever and list replaced by error; one re-armed timer, stops off-show/destroy; pinned by "goes on being read when one read fails, and says so" | leave-with-reason |
| D6 | `history-tab.component.ts:95,184` `deleteProblem` | 3 | P2 | refusal such as "still going" stays above the list after the run ended until another delete; pinned by "forgets an earlier refusal…" | leave |
| D7 | `.again` button style hand-copied in 10 files / 14 elements; `.sr-only` in ~12 | 7 | hygiene | `grep -rln "\.again {"` → 10 files all in options-backtest; per-component `.sr-only` is repo convention | leave (refactor, no defect) |
| D8 | `leg-builder-card.component.scss:3` | 2 | hygiene | = B | fix (delete line) |
| D9 | `history-tab.component.ts:20-29` class doc omits Compare and "Show older runs" | 7 | hygiene | harmless | leave |

Lens 6, History specs (56 tests):
- confirmed duplicates: `history-tab.spec.ts` L133–149 (survivors stronger: `run/run-panel.spec.ts:511-528`, `page.history.spec.ts:132-151`); L164–175 `it.each` cancelled/interrupted (same strings pinned by `run/run-panel.spec.ts:406-418`; interrupted also `page.poll.spec.ts:89`)
- probable duplicates (RunPanel rendered through History; History wiring stays proven by L211, L177): `history-tab.spec.ts` L151–162 (survivors `run-panel.spec.ts:259`, `run-failure.spec.ts:55`); L104–119, L121–131 (survivors `run-panel.spec.ts:17,136,465`, L211); `history-tab.poll.spec.ts` L117–130 (survivor `run/run-panel.poll.spec.ts:27`); `history-tab.compare.spec.ts` L56–62 (duplicated by `history-tab.spec.ts` L59 and every `td[4]`); `history-tab.compare.spec.ts` L39–44 (header copy + aria-labels every other test queries)
- keep: `history-tab.spec.ts` L367, `compare.poll.spec` (deliberate-breakage `7fd86c03`), `exact-dollars`; fixtures `runWithoutSetup`, `SETUP_CANNOT_OPEN_REASON` stay (5 other users)
- a11y-only, owner-ruling never cut: `history-tab.compare.spec.ts` L179–199 (two axe tests; the whole describe), L85–92; `history-tab.spec.ts` L191–209, describe "deleting a run, and the keyboard" L325–365.

### E. Skipped/gated

Frontend slice: none. `tests/routers/test_options_backtest_runs_endpoints.py`: 98 passed, 10 skipped without Postgres (L320, L1237, L1265, L1292, L1312, L1328, L1356, L1394, L1408, L1421; "POSTGRES_URL not configured") — the only tests proving list order/cap against a real table, 404/409 on delete, and the 204. Driver's Postgres run owns them (3,652 passed there). Route-level 422 tests and the "reads liveness in one worker-thread call" tests (L973, 1112–1160) run without a DB and pass.

### F. Not read

Python side only at interfaces: `routers/options_backtest_runs.py` full, `schemas/options_backtest_runs.py` L612–775, `saved_runs.py` L425–500, L680–750. `broker.types.ts` only the consumed schemas. Page TS/HTML/SCSS, `setup-draft.ts`, `setup-build.ts`, `duration.ts`, `data-check-view.ts`, `setup-summary-checker.ts`, `run-launch.ts` head, `run-panel.component.*`, `run-result.component.*`, `pill-choice.component.ts` read only to resolve callers. Not opened: `testing/fake-jobs.ts`, `testing/page-actions.ts`, `testing/run-fixtures.ts`, `switch.component`. Cards not viewed in a browser.

## F3 — Frontend `past-chain/` (49 files) — read at d1fe80ce

Headline: no P0/P1. All 49 files read, 18 specs run: 387 passed in 13.5 s (worktree root printed). Everything reachable in checked-in config (route has no flag; Data Lab Options companion needs only its checkbox).

### A. Feature table

| # | feature | requirement | entry point | normal/empty/failure | class | evidence |
|---|---|---|---|---|---|---|
| 1 | Past chain as 3rd lazy tab of the page | #3030, ADR 0078 d14/16/23 | `options-backtest-page.component.html:84-88`, `PastChainTabComponent` | `page.spec:19`; `day.spec:11`; `day.spec:143-230` | implemented | 387 passed |
| 2 | Day listing: expiries, tags, counts, rows left out | #3030 | `PastChainService.day` → `GET …/past-chain/day` | `day.spec:44-139`, `view.spec:10-70` | implemented | no Pydantic/OpenAPI drift (script `F3/schema_drift.py`) |
| 3 | One expiry at an instant | #3030 | `.expiry` → `GET …/expiry` | `chain.spec`, `controls.spec`, `rows.spec`, `money.spec` | implemented | specs |
| 4 | Refusals placed at their control | #3030 | `past-chain-problems.ts` | `problems.spec`, `controls.spec:307-340`, `:446-470` | implemented | specs |
| 5 | Race safety + hold last answer while same question re-read | #3030 | `heldAnswer`, resource params | `stale.spec` (7), `held.spec` (8), `download.spec:212-238` | implemented | specs |
| 6 | Download missing data, one press | ADR 0078 d16/23 | `.download` → `POST …/past-chain/download` | `download.spec:84-125`, `service.spec:80-121`, `page.spec:164-233` | implemented (frontend half; live secret path not exercised) | `contracts/data-plane-control-surfaces.json:12` |
| 7 | Multi-press download ("Still missing after it… Press again") | commit d3b99134 | `downloadPlan` `remaining`/`callTakes`, `planWords` | `view.spec:429-459`, `download.spec:484-572` | **implemented but unreachable against shipped server constants** (C1) | `MAX_CONTRACTS_PER_DOWNLOAD = MAX_WINDOW_CONTRACTS = 60` (`chain_download.py:77`), window >60 refused before pricing (`past_chain.py:515`) |
| 8 | `fetch_refusal` ("session not over") replaces download offer | d3b99134 | `PastChainExpiriesComponent` | `download.spec:399-482` | implemented | specs |
| 9 | Page integration: `limits` in, `(downloaded)` → Check data stale | #3030 | `chainDownloaded()` | `page.spec:61-78`, `:164-209` | implemented | specs |
| 10 | Data Lab Options companion hosts the tab | #3087 / #3131 | `export.component.html:170` | `export.component.spec:209-220` | implemented | `export` + `validate` specs 19 passed |
| 11 | Old Data Lab inspector gone | #3087 | — | — | confirmed gone, 3 remnants (D1–D3) | caller searches below |
| 12 | "This page does not know this state…" guards | fail-loudly | `cellOf`, `underlyingViewOf`, `chainOf` | `view.spec` via casts | unverified vs server (closed enums) | cast tests only |
| 13 | Underlying picker backfills a symbol the lake lacks | ADR 0066 | shared `app-symbol-picker adjustmentMode="raw"` | `controls.spec:429` (copy) | unverified (shared) | — |

Old-inspector survivor search: `past-chain-inspector`, `PastChainInspector`, `fetchPastChain`, `PastChainResult`, `formatOcc`, `services/past-chain.service` → 0 hits. `getOrFetchStockAggregates` alive (`strategy-builder.component.ts:223`). `utils/occ-ticker.ts::parseOccForDisplay` used by strategy-builder; `parseOcc` exported, used only by `parseOccForDisplay` and its spec. Backend/scripts/.github 0 hits for "past-chain". Kill lists / known-gaps: nothing naming this feature.

### B. Cut candidates (all confirmed; test-support/type-only)

**B1. `past-chain/past-chain.types.ts` lines 12, 20, 22, 28** — aliases `ListingState`, `PriceState`, `UnderlyingState`, `DownloadReason`. `git grep -n -w -E "ListingState|PriceState|UnderlyingState|DownloadReason" -- Frontend ':!Frontend/src/app/api/broker.types.ts'` → only the four definitions. Orphans nothing.

**B2. `past-chain/testing/fixtures.ts:19-20` `NOON_MS` + doc comment** — `git grep -n -w NOON_MS -- Frontend` → only definition (`NOON_MINUTE_ET` in `session-time.ts` is different).

**B3. `past-chain/testing/fixtures.ts`: `ContractWanted.expiry?` (line 86) and `wanted.expiry ?? '2026-10-16'` (line 92); `occSymbol(…, root = UNDERLYING)` (line 74)** — no `contract({… expiry …})` call; only call site (line 92) passes three args. `UNDERLYING` stays used at 49, 132, 162.

### C. Not cut

- C1 multi-press download machinery: `DownloadPlan.remaining`, `callTakes` slice in `downloadPlan`, `remaining !== 0` branch of `planWords`, `view.spec:429-459`, `download.spec:484-572` (5 tests). Unreachable while both server constants are 60; live only if download cap is lowered below window cap. Owner ruling; touches F1/B2's `max_contracts_per_download` limits field. The `callTakes` sentence ("The most contracts one call takes: 60.") is reachable and stays.
- C2 wire fields served, read by no Frontend code: `PastChainContract.steps_from_the_money` (asserted by `test_past_chain.py:548`, `test_options_backtest_chain_endpoints.py:361`), `PastChainSide.right`, `PastChainSide.at_the_money_strike`, `PastChainDayResponse.underlying`, `PastChainExpiryTag.version` (maybe provenance), `PastChainDownloadResponse.session_close_ms`. B2's models; OpenAPI + `broker.types.ts` regen.
- C3 `validate.component.html:108-114` collapsed `<details>` "Options past-chain inspection" linking to Build dataset — UI copy; owner ruling (D2).
- C4 `focusResult()`, `#result`, `tabindex=-1`, `nameOf/dateId/buttonId`, `aria-*` asserts — a11y class, never cut.

### D. Findings (no P0/P1)

Clean: zero hits for `new Date`, `Date.parse`, `Intl`, `DatePipe`, `toLocale*`, `toISOString`, `any`, `eslint-disable`; date-anchored values use `date-et`, instants `et`; prices/strikes stay strings through `authoredUsd` (`money.spec`); no client-side OCC parse (only the test helper `fixtures.ts:occSymbol`); hand-written wire types only alias `broker.types.ts`, zero drift; eslint + money lint guard pass ("14 shapes … on 16 money surfaces").

| path::symbol | lens | sev | evidence | disposition |
|---|---|---|---|---|
| `past-chain-time.ts:14` `tradingDateMsOf`, `past-chain-tab.component.ts:213` `ngOnInit` | 5 | robustness | `startingDate` written into the model unvalidated (bypasses `checkDate`); `etMidnightMs` throws on `0000-xx-xx`; only host is Data Lab `analysisDate` from `toISOString` | leave |
| `past-chain-download.component.ts:54-56` `kindLabel` | 3 | hygiene | non-listing/non-underlying kind labelled "Contract"; enum closed | leave |
| `past-chain-view.ts:40-44`, `past-chain-held.ts:25` | 2/3 | hygiene | `'local'` status branch nothing sets; for `ResourceStatus` exhaustiveness | leave |
| `past-chain-table.component.html:9` | 3 | copy | server `WINDOW_TOO_WIDE` at the default (empty) strikes box reads "the strikes box holds something it cannot be asked with" while empty | leave |
| `past-chain-tab.component.ts:65-72`, `:142-145` | 5 | hygiene | resource loaders ignore `abortSignal` (repo-wide pattern; one non-test use `alpaca-deploy-workflow.component.ts:473`) | leave |
| time displays | 4 | owner-ruling | bar-close instants and `at_ms` render `et`, not `local`; consistent with NY-time box; owner memory prefers Chicago-local | owner-ruling |
| D1 `docs/architecture/adrs/0022-…md:104` amendment (f) | 7 | hygiene | says Data Lab past-chain analysis date "feeds OCC symbol synthesis"; inspector + `formatOcc` deleted in #3131 | owner-ruling (one clause) |
| D2 `validate.component.ts:52` docblock, `validate.component.html:108-114` | 7 | hygiene | still says "past-chain inspector"; link-only stub, no spec pins it | owner-ruling |
| D3 `export.component.ts:386-389` `analysisDate` | 7 | hygiene | re-implements `utcMsToIsoDate` (`data-lab-request-mapper.ts:16`), same result | leave |
| `past-chain.scss` | 7 | hygiene | one sheet loaded by 6 components; no dead selector | leave |
| `past-chain.service.ts:38-64` | 7 | hygiene | `PastChainRefusedError` shape-identical to `SetupRefusedError`/`RunRefusedError` (`options-backtest.service.ts:40,:55`); `toInvalid` near copy — refactor, no defect | leave (pointer F1) |
| `testing/render-chain.ts:99` `assertNewYorkTimes` vs `testing/page-actions.ts:100` `expectTradingDatesInNewYork` | 7 | hygiene | two helpers for same idea; first is a superset | leave |
| `testing/deferred.ts` | 7 | hygiene | imported by `history-tab.paging.spec.ts:6`, `options-backtest-page.files.spec.ts:6` (F1/F2); `past-chain-download-flow.spec.ts:10-18` carries a local copy; four `run-detail` specs (F4) carry their own | fix the local copy only; rest pointers |

Test bar (lens 6; survivor read):
- `chain.spec:176-181` ≡ `rows.spec:113-117` (confirmed) — duplicate
- `undrawn.spec:41-45` ≡ same line (confirmed) — duplicate
- `controls.spec:110-126` ⊂ `controls.spec:76-93` (confirmed) — duplicate
- `past-chain-time.spec.ts:10-25` (3 tests) ≡ `shared/date/et-midnight.spec.ts:6-19`; keep `:27-29` (confirmed)
- `download.spec:60-64`, `:66-70`, `:72-81` ≡ `view.spec:487-489` + `view.spec:408-412`; integration kept by `download.spec:54-58`, `:514-518` (probable)
- `undrawn.spec:101-105` trivial (probable); `undrawn.spec:178-185` trivial/cannot fire, ≡ `chain.spec:38-47` (probable)
- `controls.spec:429-434` copy pin of static note text (probable)
- `download.spec:415`, `:479` tombstone assertions for retired sentences; consider `download.spec:465-481` title "is no longer learned" (probable)
- `limits.spec:67-75` ≡ `past-chain-controls.spec.ts:40-45` (probable)
- `controls.spec:346-357` + 14 other `setTimeout` sleeps (20-100 ms): 100 ms sleep in a 400 ms debounce can fail under 2 vCPU; negative asserts after 20-30 ms can pass vacuously — robustness
- a11y specs (`past-chain-tab.a11y.spec.ts` 17 tests, `rows.spec:119-125`, `chain.spec:353-363`, `page.spec:100-112`, `download.spec:183-190`, inline aria-pressed/aria-invalid) — owner-ruling, never cut
- `page.spec:33-59` ("no bar of setup actions", Shift+Enter) is F1 behaviour — pointer
Timing: heaviest `page.spec` 10.1 s; folder ~42 s across shards (fine).

### E. Gated/skipped

No `.skip/.todo/.only/runIf`; nothing DB-gated. Vacuous: `undrawn.spec:178-185`; negative-wait asserts under load. `a11y.spec` runs axe in jsdom with `color-contrast` disabled.

### F. Not read

Python tests (B2); `chain_download.py` beyond cap constant; `session-time.ts` beyond `parseClockText`/`MS_PER_MINUTE`; internals of `TimestampDisplayComponent`, `SymbolPickerComponent`, `extractServerMessage`; page root beyond tab binding; `strategy-builder.component.ts` beyond the occ-ticker import.

## F4 — Frontend `run-detail/` (67 files) — read at d1fe80ce

Headline: no P0/P1. Slice mostly live; four routes each have one caller. 20 spec files, 475 tests, 0 failed, 0 skipped; eslint clean; nothing DB-gated.

### A. Feature table

| # | feature | requirement | entry point | reachable | normal / empty / failure | class | evidence |
|---|---|---|---|---|---|---|---|
| A1 | Paged trades table, filter by day and leg | #3030 | `trades/run-trades.component.ts` (used at `run/run-result.component.html:14`) | yes | normal; empty sentences; refusal in server words + retry; stale answer dropped | implemented | `run-trades.component.spec.ts` (29), `run-detail.service.spec.ts` |
| A2 | CSV export of every trade | #3030 | `export/run-export.component.ts` | yes | normal; busy; refusal | implemented (browser save unverified) | `run-export.component.spec.ts`; FileDownloadService faked |
| A3 | One-day view: stored facts, events, levels/fills/marks, legend | #3030, #3109 | `session/session-view.component.ts` | yes | normal; empty-day; skipped day; withheld series; setup-unreadable; refusal+retry; late answer dropped | implemented | `session-view.component.spec.ts`, `.selling.spec.ts`, `.exact-dollars.spec.ts`, `timeline-legend.component.spec.ts` |
| A4 | One-day timeline chart | #3030 | `timeline-plan.ts` → `session/timeline-chart.component.ts` | yes | broken lines, lone minute, withheld, unplaceable covered | implemented at library seam; canvas drawing unverified (lightweight-charts mocked) | `timeline-plan.spec.ts`, `timeline-chart.component.spec.ts` |
| A5 | Two-run comparison | #3030 | `compare/compare-view.component.ts` (used at `history/history-tab.component.html:4`) | yes | normal; same settings; no shared session; 404/409 server words; retry; late dropped | implemented | `compare-view.component.spec.ts`, `.exact-dollars.spec.ts` |
| A6 | Open a trade's/row's day in the one-day view | #3030 | `day-request.ts` → `session-view [request]` | yes | new object per press; run change resets | implemented | `trade-table.component.spec.ts`, `session-view.component.spec.ts` |
| A7 | Exact-dollar discipline (server strings never numbers) | #3030 | `series-geometry.ts` one coercion file | n/a | | implemented | `run-detail.contract.spec.ts` (13); eslint CHART_GEOMETRY `Frontend/eslint.config.mjs:138,212,223` |

Route coverage: router has four routes, each with one caller here (`trades`, `sessions/{open}`, `trades.csv`, `compare`). Run change resets the day (`run-panel.component.ts:179`).

### B. Cut candidates

**B1. `run-detail.types.ts` six exported aliases with no importer** — `TradeFill` (L9), `TradeMark` (L10), `SettingDifference` (L26), `RangeDifference` (L27), `SharedSession` (L28), `RunTotals` (L29). `git grep -n -w <sym> -- . ':!Frontend/src/app/api/broker.types.ts'`: no TS importer in `Frontend/src` or `Frontend/tests` (three also hit unrelated Python classes of the same name). Confirmed. Orphans nothing (generated types stay).

**B2. `session/session-tables.scss:14` `.rows .none` rule** — no `none` class in the two templates that use the stylesheet (`session-drawn.component.html`, `session-events.component.html`); InstrumentComponent has none. `trades/trade-table.component.scss:9` `.none` is used — keep. Confirmed.

### C. Not cut

- C1 `filenameOf` starred-name branch (`run-detail.service.ts:106-126`, `starredName`, `FORMAT_CONTROL`, `isControl`, starred half of `safeName`) + ~60 spec lines: server only ever sends `filename="options-backtest-run-<int>-trades.csv"` (`trade_export.csv_file_name`, router L217) so RFC 8187/bidi handling cannot fire on the checked-in server; it is called, so not dead by AGENTS.md. Owner ruling.
- C2 exports used only internally/by specs (`filenameOf`, `groupedWhole`, `BAR_MS`, `contractWords`, `UNPLACEABLE`, `NO_REASON_GIVEN`, `NO_MINUTES`, `FILL_WORDS`, `MARK_WORDS`, `OWED_MARK_WORDS`, `isSoldLegFill`, `PlanPoint`, `PlanSlot`, `SeriesRole`, `MarkerShape`, `EventRow`, `LevelRow`, `MarkRow`, `RoomedRange`): live names; un-exporting is a restyle.
- C3 served-but-never-read wire fields (D2): backend + OpenAPI regen; money trail fields keep.
- C4 `PanePlan.caption`, `SeriesPlan.id`, `SeriesPlan.role` read only by specs (~30 spec lines to rewrite) — not delete-only.
- C5 four `.sr-only` copies — repo idiom, accessibility, out of scope.

### D. Findings

No P0/P1.

D1 test bar (survivors read; all fix=delete, orphan nothing):
- `timeline-plan.spec.ts:201-207` ≡ `session-view.component.spec.ts:267-277`
- `timeline-plan.spec.ts:435-443` ≡ `session-view.selling.spec.ts:62-71`
- `timeline-plan.spec.ts:273-281` ≡ `timeline-chart.component.spec.ts:334-349` (keep plan `:302-307`)
- `timeline-chart.component.spec.ts:105-114` subset of `:334-349`
- `trades/run-trades.component.spec.ts:375-382` weaker than `:50-57` + trade-table spec
- `words.spec.ts:29-31` asserts wording `shared/option-contract/option-contract.component.spec.ts:64` owns
- `words.spec.ts:76-81`, `:83-90` re-state `it.each` at `:67-74`
- `words.spec.ts:55-57`, `:61`, `:105` copy pins of LEVEL/FILL/MARK words (keep distinctness invariants `:52-54`, `:62`, `:106`) — trim
- `compare-view.component.spec.ts:256-264` subsumed by `:244-254` + `:225-242`
- `trades/trade-table.component.spec.ts:152-157` — `:123-130`, `:145-150` cover binding; `shared/timestamp/timestamp-display.spec.ts:113-122` owns Tokyo case
- `timeline-plan.spec.ts:466` fixture self-check line — drop
Keep: three `*.exact-dollars.spec.ts`, `run-detail.contract.spec.ts` (lint-as-test + "patterns still fire" group).
A11y specs owner-ruling, never cut: `session/session-view.a11y.spec.ts` (6), `trades/run-trades.a11y.spec.ts` (5), keyboard group `session-view.component.spec.ts:460-516`, `compare-view.component.spec.ts:267-275`, aria/tabindex asserts in legend/chart/trade-table/export specs. `.claude/rules/angular.md` still says "All UI must pass AXE"; AGENTS.md (2026-10-12) wins.
Other: `session-view.component.spec.ts:406` title overclaims (only the 500 case); `Frontend/ci-spec-durations.json:269` records `run-trades.component.spec.ts` at 17.25 s in CI vs 4.7 s local (pointer only).

D2 contract (lens 5): served by Pydantic + typed in `broker.types.ts`, read by no production code in `Frontend/src`: `OptionsBacktestTradeResponse.multiplier/.exit_group/.exit_kind` (money trail; CSV has its own exit_kind), `OptionsBacktestTradesResponse.run_id/.limit`, `OptionsBacktestSessionResponse.run_id`, `OptionsBacktestComparedRunResponse.first_session_open_ms/.last_session_open_ms/.sessions_requested/.fee_policy`, `OptionsBacktestSettingDifferenceResponse.setting/.leg_id`. Owner-ruling; waits on B3 + OpenAPI regen.

D3 time (lens 4): every instant in the folder is `mode="et"` (11 template sites + chart axis/crosshair/tooltip `timeline-chart.component.ts:117,201,211`); `temporal-rigor.md` says `local` default for trades/fills/event timelines, `et` for session views; owner memory says local for fills/events. Deliberate, labelled "ET", spec-asserted. Owner ruling. Clean: no `new Date`/`Date.parse`/`toLocale*`/`Intl.DateTimeFormat`/`DatePipe`; no string-typed `*_ms`; session open/close from server; `gridOf` (`timeline-plan.ts:151`) assumes 60 s bars and ≤24 h only. Hygiene: `timeline-chart.component.ts:33` third file-local `'America/New_York'` literal (others in `shared/date/et-midnight.ts:11`, `shared/timestamp/timestamp-display.ts:24`) — leave.

D4 silent failure (lens 3): `timeline-legend.component.ts:15-18` `cssColour` `'currentColor'` fallback unreachable (closed union) and would hide a missing token (hygiene, leave). `run-trades.component.html:22-41` after a refused "Next page" only "Try again" shows, retries the bad offset; only a filter change gets back to page 1 (P2 robustness, traced, not reproduced, leave). No stale-as-current reads (not the #3180 class).

D5 boundaries (lens 5): `shared/file/file-download.service.ts:13-20` revokes object URL immediately after `link.click()` (FileSaver delays for Safari/older Firefox; robustness, no repro). Large runs bounded: trades paged at 100; `MAX_RANGE_SESSIONS` 1,300 (`options_backtest/limits.py:7`). No `any`/`@ts-ignore`. Both catch blocks rethrow non-expected errors.

D6 hygiene: comments cite only #3030/#3109; no plan/PRD/rule-file citations, no TODO, no console, no eslint-disable. `timeline-room.ts:64-65` writes `0 - bars` to dodge the money lint's minus selector (note).

### E. Gated / skipped

None skipped; none DB-gated; none that cannot fire. Gap: no spec renders the real chart canvas.

### F. Not read

Skimmed only: `testing/detail-fixtures.ts`, `testing/selling-fixtures.ts` bodies; `timeline-plan.spec.ts` 1-18. Not read: Python run_detail/comparison/session_*.py + their tests (B3); other F slices except call sites; shared timestamp-display/option-contract/authored-usd implementations (specs only).

