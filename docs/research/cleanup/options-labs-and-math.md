# Clean-up — Options labs and options math: chain, pricing lab, strategy builder, volatility (#3155)

Findings for ticket [#3155](https://github.com/tim1016/learn-ai/issues/3155) under map [#3146](https://github.com/tim1016/learn-ai/issues/3146) (tracker #3145). **Read at `f1288f2d`** (origin/master, 2026-10-14 ~13:16 CDT). This file lives on the throwaway branch `research/cleanup-options-labs-and-math` and is never merged. The delete-only cut is [#3189](https://github.com/tim1016/learn-ai/pull/3189).

## Result

- **No P0. Two P1s and one P2 filed at once:**
  - [#3186](https://github.com/tim1016/learn-ai/issues/3186) (P1): Strategy Builder's Max Loss, Max Profit and breakevens come from a ±5 % window around the strike, so a short put 590 @ 5 shows a loss of $2,450 (the Analysis card, ±30 % of spot, shows about $17,200) against a real $58,500. Reproduced by replaying the component's own functions against the Python canonical function.
  - [#3187](https://github.com/tim1016/learn-ai/issues/3187) (P1): `POST /api/quantlib/compare` (and `/price` with Monte Carlo) run QuantLib on the data plane's event loop, fired by every Pricing Lab contract pick. A 100-point compare held the loop 140 s (6 s with other inputs); a Monte Carlo `/price` had not returned after 75 s. `IBKR_BROKER_ENABLED` is set only on this process (`compose.yaml:220`) and ADR 0053:149 says a loop stall becomes a forced feed disconnect. The stall is reproduced; the disconnect is not, so the owner may rate it P2.
  - [#3188](https://github.com/tim1016/learn-ai/issues/3188) (P2, filed because it discloses a credential): `POST /api/options/expirations` returns the Polygon API key in its 500 body when the vendor answers 4xx. Reproduced offline.
- **One broken feature, not filed as a bug because it still needs one live call:** the live IV30 routes (`/api/edge/iv30/vix-style`, `/parametric`) and the opt-in IV recorder call `list_snapshot_options_chain` without an expiry; the client defaults the snapshot to **today's** expiry and the routers drop 0-day expiries, so against the real client they answer 502 "got 0". The default is from 2026-02-17 (`36f68915`), the route from 2026-04-27 (`d6576bcd`); tests mock at the client boundary so the default is never exercised. Reproduced under a fake SDK that applies the vendor's exact-date filter (R5 D-1); the running `polygon-data-service` has no `/var/lib/iv-recorder` (recorder not enabled there), so the owner's recorded history (ADR 0071 d7 "recorded since 2026-04-28") must be checked on the machine that has it. First item of handoff slice H5.
- **One cut PR, #3189:** 8 pieces of Python (1 private dataclass field, 1 unused keyword, 1 constant, 1 preview field, 1 enum member, 1 hash helper with its 2 echo tests, 1 builder method, 1 never-read surface field) and the Frontend leftovers (the stub Volatility page with its route and tab, never-rendered chain row fields, four unread strategy-builder row fields, dead preset-card CSS, three unread payoff-chart inputs, one unread display part, one dead rule). 24 files, **+8 −285**, no contract, OpenAPI, GraphQL or fixture change. None of it is validated volatility or options math (◇).
- **For handoff (#3159):** 11 fix slices (H1–H11), a lens-6 test-bar list (about 20 tests, each naming its survivor), 16 owner rulings, and cross-module pointers.
- **Feature coverage:** 72 feature rows over the seven slices. Partial or broken: Strategy Builder numbers (F1), Options Lab chain header change (F2 D2) and silent poll (F2 D6), Pricing Lab compare (R5 D-6..D-9), live IV30 and recorder (R5 D-1). Missing by design: Strategy finder and Volatility tabs (placeholders; Volatility goes in the cut), the L/S "add leg" column (a no-op). Unverified by design: the Alpaca-side judgment of option buying power (ADR 0076:953), multi-leg and Live behind default-off flags, the resolver on a real chain (ADR 0076 d8), the opt-in recorder.

## Method

Seven read-only readers, split by sub-package (never by lens), each briefed from one file with the lenses, the Sacred list, the kill lists *with their comments* (#2703, #2704, #2724, #2725, #2727, plus #2705, #2706, #2709, #2726, #2729–#2731, #2733, #2734, #2737, #2744, #2747), the pointers, the method rules from earlier tickets, and an inline report format:

| id | slice |
|---|---|
| R1 | `app/options` part 1: `contract`, `expiry`, `exact`, `whole_units`, `lean`, `quotes`, `model_input_reasons`, `named_structures`, `structure`, `structure_catalogue`, `structure_check`, `order_terms`, `resolver`; their tests |
| R2 | `app/options` part 2: `american`, `at_expiry`, `bounds`, `decay`, `deadlines`, `simulated_fill`; their tests; AM-00x fixtures |
| R3 | `app/options` part 3: `entry`, `entry_came_to`, `preview`, `requirement`, `buying_power_check`, `event_allocation`, `watch`; their tests; OSM/OEV fixtures |
| R4 | `app/volatility` (11 files), `tests/volatility`, `tests/edge/test_iv30_*`, IV/RV/OPT-IB fixtures, generators |
| R5 | routers `options`, `quantlib_options`, `iv30`, `iv_recorder` and the services behind them (`iv_recorder`, `quantlib_pricer`, `bs_greeks`, `rate_dividend_service`, `risk_free_rate`, `options_companion_service`), their tests, Backend/Frontend callers |
| F1 | Frontend `strategy-builder`, `utils/black-scholes*`, parity fixture and generator, `payoff-chart` |
| F2 | Frontend `options-lab` (shell, chain, stubs), `options-chain-v2`, `pricing-lab`, `quantlib.service` |

Their reports are appended below, unchanged except where **Driver's reconciliation** says otherwise. The driver re-verified every cut candidate with a whole-repo `git grep -w` (generated Frontend types included), read the call sites, and re-read the P1 candidates (F1 D1: `strategy-builder.component.ts:629-806`; R5 D-1: `polygon_client.py:751-757` and `iv30.py:195`; F2 D2: `polygon_client.py:779-780` against the SDK model; D21: `quantlib_options.py:435-566`).

## Evidence

| run | result |
|---|---|
| Readers' slice runs at `f1288f2d` | R1 312 passed (0 skipped); R2 266; R3 362 + 62 fixture/parity; R4 207 + 21; R5 about 530 over 15 files incl. `test_bs_cross_engine_parity.py` 361; F1 82 (3 spec files); F2 21 (4 spec files). 0 skipped anywhere in the module |
| Sandbox-only failures | `tests/services/test_option_search.py` 8 real-worker tests: `PermissionError` creating multiprocessing semaphores sandboxed; 60/60 pass unsandboxed |
| After the cut (driver) | Python: `tests/options` + `tests/volatility` + `test_ibkr_iv_fixtures.py` + `test_volatility_fixtures.py` + `test_option_preview_views.py` + `tests/edge/test_iv30_and_vrp.py` + `test_iv30_stability.py`: **1,179 passed**; `ruff check` on the touched trees: clean. Frontend: `tsc -p tsconfig.json` and `tsconfig.spec.json` clean; `eslint --max-warnings 0` on the touched trees clean; 9 spec files (strategy-builder, options-lab-chain ×2, pricing-lab, occ-ticker, app.routes, app.component, black-scholes ×2): **201 passed** |
| `scripts/export_openapi_contract.py --check` | exit 0 (R5) |
| Parity tests really run | `test_picking_parity.py` 218 cases (R1), cover-book 8, `test_bs_cross_engine_parity.py` 361, AM-001 400, AM-003 140, AM-004 400, OSM-001 27, OSM-002 10, OEV-001 17, IV-001..RV-004 (R4 §Q2), OPT-IB-002 2,332 rows, Frontend BS parity 360 cases |
| Lens ruff set (`S110,S112,BLE001,E722,T201,DTZ003-7,F401,F841,ERA001`) | clean on `app/options`, `app/volatility`; `quantlib_options.py` DTZ011 ×2 (`:266,:452`), BLE001 ×1 (`:537`); `quantlib_pricer.py:148,336` DTZ011 |

## The cut — PR #3189

| file | piece | evidence |
|---|---|---|
| `app/options/resolver.py` | `_Market.decision_date` (field and its one assignment) | `git grep -nw decision_date`: only those two lines |
| `app/options/bounds.py` | `bounds_of(step=)` | 5 app call sites + 2 test calls, none passes `step=`; catalogue builds its own steps |
| `app/options/event_allocation.py` + test | `EVENT_CLOSE_PRICE` | production writes literal `Decimal(0)` (`clerk/sqlite/option_events.py:1747,2450`); only reader was `assert Decimal(0) == EVENT_CLOSE_PRICE` |
| `app/options/preview.py` + 2 tests | `PickedLeg.multiplier` | `leg_view` never reads it (the view reads `size.multiplier`); only `test_preview.py:128` did |
| `app/volatility/solver.py` + test | `SolveStatus.OK` | nothing returns it; its one reference was a set member in a fixture test |
| `app/volatility/conventions.py` + test | `SurfaceConventions.to_hash_dict` and `TestSurfaceConventionsDataclass` (2 echo tests) | callers cut with the surface router in #2755 (`38474712`); `git grep -w` returns only the tests |
| `app/volatility/surface.py` | `VolSurfaceBuilder.build_bid_ask`; `eval_date` on `VolSurface` and the builder | `build_bid_ask`'s only caller was the router request field cut in #2755; `.eval_date` has no reader; both construction sites use keywords |
| `options-lab/volatility-stub/`, `app.routes.ts`, `options-lab.component.html` | stub Volatility page, route, tab | map #2700 ◇: "…the stub Volatility page go"; `git grep -nw VolatilityStubComponent`: route + own file; no spec, no e2e |
| `options-lab-chain.component.ts` | `ChainRow.distance`, `distancePct`, `formatSigned` | no template, spec or other reader |
| `strategy-builder.component.{ts,html,scss}` | `callPriceNum`, `callIv`, `putPriceNum`, `putIv` row fields; the pre-UX-Q4 preset-card CSS (`.preset-grid`, `.preset-group-divider`, `.preset-card`, `.template-select`, `optgroup`) | `git grep -w`: only the assignments; the classes appear nowhere in markup |
| `shared/payoff-chart/payoff-chart.component.ts`, strategy-builder html | inputs `breakevens`, `spotPrice`, `timeToExpiry` and their three bindings | the chart reads none (`grep` over its .ts and .html); one caller; the parent's own signals stay |
| `utils/occ-ticker.ts` + spec | `OccDisplayParts.expDateShort` | `git grep -w`: spec + definition only |
| `pricing-lab.component.scss` | `.tt-diff` | the tooltip markup emits `tt-title`, `tt-row`, `tt-dot` only |

## Driver's reconciliation (what changed between the readers' reports and the cut)

- **Held out of the cut by the map's lens-2-only rule:** every duplicate or weak test deletion (lens 6), comment/doc/docstring drift, and everything a reader marked "edit" rather than "delete" (F1 B5 `presetGroups` labels, B6 `SelectedHistoryContract` fields, B7 `LegConfig.enabled` and its ten filters, B8 the Frontend half of the unused analysis flags).
- **Test-only production helpers left, per the #3150 precedent** — the tests that call them prove behaviour that stays: `deadlines.entry_cutoff_ms`, `watch.LinePlan.lines`, `whole_units.WholeUnits.beyond`, `event_allocation.OptionEventAllocation.{kind,contract,contracts,multiplier,share_quantity_change,cash_change_cents}`, `LotAllocation.remaining_quantity`, `decay.StructureDecay.{per_share,per_structure,estimated}`, `decay.LegDecay.detail`, `american.AmericanDailyDecay.later_price_is_exercise_value`, `american.AmericanIv.detail`, `resolver.LegEvidence.*`/`Evidence.*`/`Candidate.*` (also ADR 0076 d8), `bs_greeks.bs_european_vega` (◇, #2703 addendum), the price-normalization and surface functions (◇).
- **`LegGapOverLimit` payload attributes (R3 B3), `app/volatility/__init__.py` re-export block (R4 B5):** left. The first is a documented exception payload whose attributes only tests read (test-only helper); the second is the package's public face with no importer, which changes what `import app.volatility.basis` pulls in; both are owner rulings below.
- **`quantlib_pricer.implied_volatility` (R5, no caller, no test):** left. `solver.py:7-8`'s provenance block names it the companion with parity "pending-fixture"; cutting it rewrites a provenance claim, and the owner's ◇ names "the IV solver". #2704 addendum 2 cut `options/pricer.py` for "Validated against: NONE", so the precedent points to cut: owner ruling 1.
- **`_options_companion_report/` (7 files), `generate_options_companion_report.py`, `validate_options_companion_timestep.py` (R5):** pointer, not rows. The map's Out-of-scope list names tracked output files and one-off generators in the `PythonDataService/` root under "Repo plumbing".
- **L/S "add leg" no-op column (F2 B3), Strategy finder stub (F2 C), `selectedEngine` (F2 B5/R5):** left. The first two are build-or-drop owner calls (no requirement names them; the Volatility stub was ruled, these were not); the third needs the literal moved, an edit.
- **`whole_units.in_leg_direction` (R1):** waits on #3163 / the clerk owner: its only production caller fills `HeldOfEntry.whole`, which only tests read — a custody read on the money path, not touched here.
- **`build_bid_ask` and `to_hash_dict` are in kept-by-◇ files** (`surface.py`, `conventions.py`). They are not math: the first wraps `build` twice for a router field that no longer exists, the second hashed the cut cache key. Both were named in #2755's own diff as the router's. The `surface.py` and `conventions.py` math is untouched.
- **Severity calls.** F2's D2 (chain header "spot change" is the first contract's `change_to_break_even`, verified against the SDK model `polygon/rest/models/snapshot.py:192`) is rated P2 (an informational figure) and left as a finding; F1's D1+D2 became #3186 (P1); F2's D21 and R5's blocking-`async def` rows became #3187 (P1); R5's D-2 became #3188 (P2). R5's D-1 stays a finding until one live call confirms it.
- **Independent review:** one independent reviewer (a fresh subagent running the repo's thermo skill on the commit, before the first push): **PASS**, no major findings. It proved each deletion group dead by whole-repo `git grep -nw` (generated types, docs, scripts, Backend, e2e), found no removed or weakened Sacred item, and found no deleted test that proves behaviour that stays. Two minors, both fixed in the commit: a missing blank line in `event_allocation.py`, and one stale test id in `scripts/pr_shard_durations.json`. The PR touches files with provenance blocks (`solver.py`, `conventions.py`, `surface.py`, `event_allocation.py`) and so counts as a math PR under the Gates although no math, fixture or "Validated against:" claim changes; 8 added logic lines, so the size tier asks for none. CodeRabbit asked; CI pending when closed.

## Handoff list for #3159

### Fix slices (each PR-sized; severity is P2 or robustness unless noted)

| # | what | where | lens | evidence |
|---|---|---|---|---|
| H1 | **Strategy Builder numbers** (#3186 P1 + the rest): analytic max profit/loss and whole-payoff breakevens; `iv <= 0` priced at intrinsic as the server does (D3); EV shown ×100 beside max P/L (D4); what-if IV floor 0.01 vs 0 (D5); expiry pick uses "now" not today's 16:00 (D14); stale time on an open page (D8); add the owed browser-vs-Python parity (Greeks, P&L, payoff, breakevens) and regenerate the BS fixture by importing `bs_greeks.py` (D6) | `strategy-builder.component.ts:629-806,968-969,608-611,838`; `strategy_engine.py:100`; `scripts/generate-bs-parity-fixture.py` | 1/4 | F1 D1–D8, #3186 |
| H2 | **Strategy Builder and Options Lab failure/staleness:** `getOptionsExpirations` drops `error` (also Pricing Lab); the chain stays under a new ribbon selection after a failed fetch (D11); no request token on `fetchExpirations` (D12); the 8-leg cap is silent (D13); unguarded `localStorage` (D16); silent poll has no as-of and overlapping polls (F2 D6, D11) | `market-data.service.ts:281-283`; `strategy-builder.component.ts:957-1012,1040`; `options-lab-chain.component.ts:244-327` | 3 | F1 D9–D16; F2 D5–D12; R5 D-5 |
| H3 | **Data plane event loop** (#3187 P1): thread-offload `/compare` and `/price`, cap `num_points` and the MC tolerance; same for `iv30.py:195-206`, `iv_recorder.py:81`, `options.py:32`, `snapshot.py:46-103` | `quantlib_options.py:434-566`, `quantlib_pricer.py:111` | 5 | #3187; R5 lens 5; F2 D21 |
| H4 | **Polygon expirations route hardening** (#3188 P2 + validation): generic 500 detail; `gte > lte` → 422; cap range; surface skipped windows; paginate or sort `limit: 1000`; a route test with a fake SDK | `options.py`, `polygon_client.py:658-737` | 5 | R5 D-2, D-3, D-3b, D-4 |
| H5 | **Live IV30 and recorder**: confirm D-1 with one live call, then request a window of expiries (or no default) and test through a fake SDK; recorder store on a volume or env path (D-10); error branches `replication_failed`/`insufficient_snapshot` untested; fabricated `log_iv_dominance_gate` fields; `read_series` scans every line; `iv30_parametric` always `None` (ADR "Owed") | `iv30.py:195,253`, `iv_recorder.py:185-311`, `polygon_client.py:751` | 1/3 | R5 D-1, D-10, lens 3/5/6 |
| H6 | **Pricing Lab / QuantLib compare correctness**: Greeks the engine does not provide set to 0.0 (D-6); `spot_min or` treats 0 as unset, `spot_max < spot_min`, engines silently skipped (D-7); `_scipy_bs_greeks` has no dividend and is a second BS copy (D-8); `date.today()` UTC vs ET (D-9, `quantlib_pricer.py:148,336`); 0DTE message says "timed out" (F2 D4); snapshot `dividendYield` ignored (F2 D17); `risk_free_rate`/spot range unbounded (F2 D20); model-card step counts (F2 D22) | `quantlib_options.py`, `pricing-lab.component.ts` | 3/4/5 | R5 D-6..D-9; F2 D4, D9, D17, D20, D22 |
| H7 | **Options Lab chain**: header spot change is `change_to_break_even` for both fields — read `stockSnapshot.todaysChange*` (F2 D2); hard-coded `SPY` badge (D1); `daysToExpiry` ceil on a 16:00 anchor (D3); unavailable IV shown as `0.0%` (D10); ribbon year in the viewer zone (D16) | `polygon_client.py:779-780`, `options-lab-chain.component.ts:111-119`, `expiration-ribbon.component.ts:27` | 1/4 | F2 D1, D2, D3, D10, D16 |
| H8 | **Volatility math guards** (all test-only today, ◇ — fix, never cut): `check_smile_arbitrage` returns `passed=True` when no vol evaluates and misses butterflies on unsorted strikes (D-3.1/D-3.1b); `compute_skew_metrics` returns 0.0 on failure (D-3.4); legacy vs normalized mid differ for zero/crossed quotes (D-3.7, IV-003 cannot see it); `SliceDiagnostics.n_failed=0` hard-coded (D-3.8); `_sabr_vol` limit (D-4.4); `basis.py` `np.int64` (D-4.7); `assert` as validation (D-5.1); NaN inputs → `CONVERGENCE_FAILURE` (D-5.2); explicit tolerances on 32 `approx` + 3 `assert_allclose` (D-4.5); silent skips in the py_vollib parity test (D-3.5b) | `fitting.py`, `analytics.py`, `vix_replication.py`, `surface.py`, `solver.py`, tests | 3/4/5 | R4 |
| H9 | **iv30 golden snapshot governance and provenance blocks**: register the iv30 parquet in `manifest.json` (`internal_regression`), drop the `skipif`, assert existence; cite the receipts in `solver.py`/`basis.py`/`vix_replication.py`/`fitting.py`/`surface.py` provenance; OPT-IB-002 docs say 1e-3 vol but `atol` is applied to price | `tests/fixtures/golden/iv30/`, `golden_support/hashing.py:82-90` | 4 | R4 D-4.2, D-4.6, D-4.8, D-3.10 |
| H10 | **`app/options` robustness**: `option_expiry_date(10**20)` raises `OSError` not a refusal (R1 D-4); `event_date_ms` skips `_require_an_instant` (R3); `entry_cutoff_on` raises `LookupError` on a no-session date on the Clerk path (R2 D2); a pricer refusal becomes the "that is usual on a contract's last full day" sentence (R2 D1); explicit tolerance on `TestTimeToExpiry` (R1 D-3) | `expiry.py`, `event_allocation.py:355,434`, `deadlines.py:149-161`, `decay.py:144-146` | 3/4/5 | R1, R2, R3 |
| H11 | **Docs and comments, mechanical**: `engine-authority-map.md:97,122` (AM-006; sold multi-leg entries), `options-companion-format.md:47,173,175,212,289`, `known-gaps.md:91-96` (no `new Date(string)` left in Options Lab/Strategy Builder), `iv-rv-basis-alignment.md:60-91`, `golden/README.md:150`, `golden/iv30/attribution.md:11`, `math-sources-of-truth.md` cites (3), `whole_units.py:18` (decision 27, not 10), comments citing plans (`black-scholes.ts:1-46`, `pricing-lab.component.ts:376-378`, `iv30_health.py`, `iv_provenance.py`, 5 test headers), step counts at `quantlib_options.py:441-498`, `main.py:1414`, `Query.cs:77`, `solver.py:156`, `fitting.py`/`__init__.py` claim QuantLib | listed in each reader's lens 7 | 7 | R1–R5, F1, F2 |

### Test-bar slice (lens 6; delete-only; every row names the surviving assertion in its section)

About 20 tests: R2 D4 `test_decay.py:160` (survivor `:179`), D5 `test_decay.py:419`; R3 `test_buying_power_check.py:243-244`, `test_entry.py:127-140`, `test_preview.py:110-113`; R4 `test_vix_replication.py::test_max_single_strike_share_in_unit_interval`, the two oracle-only meta re-reads, `test_iv30_stability.py::…test_target_tenor_changes_baseline_score`; F1 D19 (`strategy-builder.component.spec.ts:504-516`, survivor `occ-ticker.spec.ts:58`), D20, D21, D22; F2 D25 (`options-lab-chain.component.spec.ts:173`, cannot fail). Accessibility-only specs are **not** in it (owner ruling 2026-10-12). Weak tests to fix rather than delete are in R4 lens 6 and F1 D22.

### Owner rulings

1. `quantlib_pricer.implied_volatility`: a second IV solver with no caller and no test; keep (◇ "the IV solver") or cut as `options/pricer.py` was (R5).
2. Strategy finder stub tab and the L/S "add leg" no-op column: build or drop (F2 B3, C).
3. `/price` engine selector (`binomial_*`, `finite_diff`, `monte_carlo`, `_numeric_*` fallbacks) is reachable only by direct GraphQL; `QuantLibService.selectedEngine`; `evaluation_date`: keep or drop with the API change across Python, C# and snapshots (R5, F2).
4. `app/volatility/__init__.py` re-exports (no importer): public face or drop (R4 B5).
5. ADR 0076 d8 "kept with the pick": must `Resolution.evidence` be kept at rest? Today it lives in memory only (R1 D-1).
6. Pricing Lab diffs against the TS pricer by default and `black-scholes.ts:21-27` still says "LEGACY-OK (intentional)" against ADR 0068:31 non-conformance; follow-up 4 of #2747 was never filed (F2 D13).
7. Snapshot `dividendYield` is fetched and ignored by Strategy Builder and Pricing Lab; both price at q = 0: intended? (F1 D7, F2 D17).
8. `fitting.py` `negative_variance` counter cannot fire and `calendar_violations` is never set (the −15 health term is always 0): build or drop (R4 D-3.2, D-3.3).
9. `iv30_atm_50d_trading_basis`, `wrap_legacy_as_opra_mid`: no caller, no test; kept by ◇; add a test when touched (R4).
10. `dte_to_ttm`/`ttm_to_dte` and the `Actual360`/`ActualActual` branches (15 tests): test-only conventions arithmetic; keep or cut (R4 C).
11. `py_vollib` (GPL, tests and generators only per `golden/README.md:33-39`) sits in the runtime image (`requirements-light.txt:17-19`): move to dev (R4 D-7.8).
12. A second `mcal.get_calendar("NYSE")` in `scripts/fixture_generators/volatility.py:128` as an independent oracle: state the reason or use the canonical module (R4 D-4.1).
13. OPT-IB-002 proves price-space inversion only; is IV parity with IBKR wanted? (R4 D-4.6).
14. Options-companion CSV `expiration` as `YYYY-MM-DD`: no ADR 0022 deviation names it (R5).
15. `deadlines.entry_cutoff_ms` and the other test-only production helpers in "Driver's reconciliation": move into tests or keep.
16. Accessibility-class code and specs seen and never cut, per 2026-10-12 (`role="tablist"` on anchors, `aria-pressed`, `aria-live`, icon-only buttons).

### Pointers outside this module

- Repo plumbing (Out of scope in the map): `PythonDataService/_options_companion_report/`, `generate_options_companion_report.py` (hard-coded `timedelta(hours=4)`, a `print`), `validate_options_companion_timestep.py`, `_spy_partial_parity_results.json`, `_validation_report.html`, `generate_validation_report.py`, `run_spy_partial_parity.py`, `study_*.txt`.
- `app/routers/edge.py:156-160` `.ffill()` on IV/RV against its own comment `:142-144` and the temporal rule (router reader).
- `app/research/options/iv_builder.py:25-27` redefines `MIN_OPTION_PRICE=0.05`, `MIN_IV=0.05`, `MAX_IV=3.0` against `solver.py:37-47`'s 0.001 / 0.005 / 5.0; `contract_finder.py`, `iv_builder.py` and `conventions.py:88-92` carry their own `dte/365` and `/365.25`; hand-typed 252 literals (`hf_realized_vol.py:146`, `hf_forward_rv.py:52`).
- `Frontend/src/app/utils/occ-ticker.ts:43` a second OCC parser (Polygon `O:` form) with no Python parity spec; `broker-options-chain.component.ts:~358` `q.iv ?? 0.2` feeds a placeholder IV into the engine-Greek column; other `requests.get(...apiKey)` sites in `polygon_client.py` may leak like #3188.
- `Frontend/src/app/shared/option-contract/option-expiry-date.spec.ts` claims parity with `expiry.py` and pins DST-change days that `test_expiry.py` does not (R1 D-5).
- Six TS6133 `noUnusedLocals` hits outside the slice: `bot-panel-shell.component.ts:304,443`, `data-lab-chart.component.ts:342`, `edge-api.service.ts:219`, `spec-strategy-runner.component.ts:523`, `shared/symbol-catalog/testing/fake-symbol-catalog.ts:70`.

## Not covered

Each reader states what it only skimmed in its F section. Nothing in this module was run against live Polygon or IBKR: R5 D-1 and D-4 each need one live call. Layout and the rendered pages were not looked at in a browser; F1 D1–D3 and F2 D2 are code-level reproductions. The tree math in `american.py` was not re-derived (◇; evidence is the fixtures plus R2's 1,300-contract fuzz). The .NET side was read only at the call spans (no `dotnet` on the host).

---

# Reader reports (transcribed from each reader's inline report, condensed; headings demoted one level)

## R1 — Python `app/options` part 1: contracts, structures, contract resolver (read at f1288f2d)

Headline: nothing dead except one assigned-never-read field (`_Market.decision_date`). Slice **312 passed, 0 skipped, 21 s**; `tests/options` whole dir 928 passed; no Sacred symbol in this slice. Other items are ADR-required, test-only, or hygiene. ruff (project and lens set) clean on the 22 files.

### A. Feature table

| feature | requirement | entry | reachable | normal / empty / failure | class | evidence |
|---|---|---|---|---|---|---|
| OCC contract identity, strike whole thousandths, adjusted-root refusal (`contract.py`) | ADR 0076 §1 | `OptionContract.of/from_occ_symbol`; `strike_thousandths` (`schemas/action_plan.py:101,136`) | yes (Alpaca adapter/clerk, `options_backtest`, schemas) | round-trips Alpaca symbols; bad root, zero/negative/too-fine strike, float residue, `bool`, symbol year outside 2000-2099 refused | implemented | `test_contract.py` 39 |
| Option expiry anchor 16:00 New York (`expiry.py`) | ADR 0022 amend. (i) | `option_expiry_anchor_ms`, `option_expiry_date`, `OptionExpiryMs` | yes (11 app importers) | summer/winter/half day; off-anchor instant refused; out-of-range ints see D-4 | implemented | `test_expiry.py` 10; DST-change days 2026-03-08 and 2026-11-01 match Frontend parity spec literals (reproduced) |
| `time_to_expiry_years`, `option_expiry_dte_days` | #2934, #2789 | `strategy_engine.py:626`, `options_companion_service.py:234`, `routers/snapshot.py:90` | yes | floors at 0 | implemented | `tests/test_strategy_engine.py::TestTimeToExpiry` (outside slice, D-3) |
| Exact decimal helpers (`exact.py`) | provenance block | `whole_units`, `exact_sum`, `require_everyday` | yes | exact under ambient prec 3; NaN/inf/1e5000000/float refused | implemented | `test_exact.py` 6 + callers |
| Whole structures / legs in step (`whole_units.py`) | ADR 0076 d27 (cited d10, D-8) | `entry_outcome.py:219`, `envelope_reservations.py:316`, `option_entry.py:621` | yes, clerk only | min-over-legs, wrong-way leg < 0, dup leg, no legs | implemented | `test_whole_units.py` 17; `test_several_legs_books.py:388-401` |
| Net-delta lean (`lean.py`) | ADR 0076 d17 | `options/preview.py:322` | preview only | hand-derived atol 1e-9; missing/NaN delta refused | implemented | `test_lean.py` 10 |
| Quote usability (`quotes.py`) | ADR 0076 d8, ADR 0067 | `quote_fault`, `receipt_times`, `NOT_YET_ARRIVED` | yes | 20-case order-of-faults table | implemented | `test_resolver.py::test_a_quote_is_usable_or_fails_as_one_thing`; `tests/broker/ibkr/test_option_quotes.py` |
| Model-input reason codes (`model_input_reasons.py`) | ADR 0076 d18 | `WhyNotKnown` | yes: all 14 members produced in `option_model_inputs.py`/`_client.py` | enum | implemented | consumers' tests |
| Named structures, resolved structure, shape check | ADR 0076 d7 (table matches code row for row) | `resolved_structure`, `require_named_shape` | yes | all 12 names; 11 typed refusals; float price refused | implemented | `test_structure_check.py` 65 |
| Structure catalogue (starts, groups, rules, order-term starts, sentences) | ADR 0076 d17 | `GET .../bots/option-structures` (`routers/broker_v2_panel.py:363`), Deploy builder | yes, read-only, Alpaca accounts | every name once in groups, ranges served | implemented | `tests/broker/v2panel/test_option_structure_preview_route.py`, `test_structure_check::test_every_starting_plan_comes_to…` |
| Order-term ranges and Clerk limits (`order_terms.py`) | ADR 0076 d17, d19 | `schemas/option_order_terms.py`, `option_admission.py` | yes | owner range inside Clerk range; non-whole refused | implemented | `test_order_terms.py` 7 |
| Contract resolver (`resolver.py`) | ADR 0076 d8 | `resolve_contracts` (`option_search.py:235`), `recheck` (`bot_option_run.py:615`), `expiry_a_rule_comes_to` (`watch.py:75,147`) | yes: preview always; bot entry Dry Run/Paper; multi-leg only with `ALPACA_MULTI_LEG_OPTION_ORDERS_ENABLED` (`config.py:298`) | all 12 refusals, rules, ties, windows, recheck | implemented at unit level; **unverified** on a real chain | `test_resolver.py` 158; `test_picking_parity.py` 218 cases (vacuity guard `> len//10`); 8 real-worker tests |
| Evidence kept with the pick | ADR 0076 d8 | `Resolution.evidence` | n/a | built and returned | **partial** downstream (D-1) | `test_resolver.py:1222,1242` |

### B. Cut candidate

| symbol (lines) | what | proof | goes with it | conf |
|---|---|---|---|---|
| `app/options/resolver.py::_Market.decision_date` (field `:475`, kwarg `decision_date=today,` `:1008`) | private dataclass field, assigned once, never read | `git grep -nw decision_date -- PythonDataService/app/options PythonDataService/tests/options` returns only those two lines; AST scan of every dataclass/pydantic field in the 12 modules for a `.attr` read found no other zero-reader field; `_Market` private, no test constructor; `today` still used by `_listed_expiries` | nothing | confirmed |

### C. Not cut

| item | why |
|---|---|
| `LegEvidence.listed_expiries_ms` (`resolver.py:311`), `.listed_strikes_thousandths` (`:315`) | read only by tests (`test_resolver.py:373,1254-1255`, `test_watch.py:162`, `tests/broker/ibkr/test_option_quotes.py:1425`); ADR 0076 d8 names these lists in the evidence |
| `Evidence.decision_bar_close_ms/.money/.underlying_quote/.watched`, `Candidate.contract/.quote/.fault` | no production reader (production reads `.legs`, `.model_spot`, `.model`, `.selection_ms`, `Candidate.passed_over/.priced/.vendor_delta`); same ADR d8 clause; `test_resolver.py:1222`; owner could rule (D-1) |
| `ContractsNotResolved.recheck`, `Recheck.underlying_quote` | read by `test_resolver.py:1367,1420,1485`, `tests/services/test_option_search.py:152`; ADR d8 recheck "keeps the generation it saw"; money path |
| `WholeUnits.beyond` (`whole_units.py:65,81`) | read only by `test_whole_units.py`, `test_several_legs_books.py:397`; test-only production field, finding |
| `whole_units.in_leg_direction` (`:84`) | only production caller `option_entry.py:625` fills `HeldOfEntry.whole` (`:580`); its only production reader `legs_out_of_step.py:32` reads `holding.legs`; `.whole` read in `test_several_legs_books.py:388,397,401,549`. **Waits on #3163 / clerk owner** (custody read, money path) |
| `ModelInputs.tree_steps` (`resolver.py:214`) | no `tree_steps=` in `app/`; production always `None`, tests pin 101; tunable knob |
| `ResolverLimits.coherence_window_ms` | default only; ADR d8 "2,000 ms unless set otherwise" |
| `structure_check._nothing_more/_long_straddle/_long_strangle/_iron_condor` unused `net_price` (ARG001 `:280,284,308`) | uniform dispatch signature in `_SHAPE_OF` — live registry |
| `whole_units.py` whole, `order_terms.MIN/MAX_*_MS`, parts of `quotes.py`, `exact.whole_units` | clerk callers (`entry_outcome.py:60`, `envelope_reservations.py:88`, `option_entry.py:66`, `option_admission.py:190`, `contract_terms.py:94`, `exit_estimate.py:231`) live today; `options_backtest` callers (`OptionContract`, `exact_sum`, `require_everyday`, `ResolvedLeg`, `option_expiry_date`) count as live; cross-check #3163 / #3185 |

### D. Finding rows

| row | lens | sev | evidence | disposition | status |
|---|---|---|---|---|---|
| D-1 `Resolution.evidence` "kept with the pick" (ADR 0076 d8) holds only in memory downstream: `bot_option_run.py:630-650` logs `picked/selection_ms/recheck_ms/generation`; `OptionEntryEvidence` has no ladder/window/candidates; no persister of `LegEvidence` (`git grep listed_strikes_thousandths` hits resolver and tests only) | 1 | robustness | | owner-ruling: does "kept" mean at rest? | traced |
| D-2 resolver validated only on a chain priced by the model's own pricer (`test_resolver.py:121-171`); ADR d8's last bullet says a frozen real chain "waits for the owner's word"; partial cover by `test_picking_parity.py` | 4 | robustness | `resolver.py:20` "Validated against: tests/options/test_resolver.py" | owner-ruling (pending in ADR) | traced |
| D-3 `expiry.py:43` names `tests/test_strategy_engine.py::TestTimeToExpiry` as validation, which asserts `pytest.approx(...)` at default tolerance (`:325-331`) | 4 | hygiene | | fix: explicit tolerance | traced |
| D-4 `option_expiry_date` raises `OSError` for \|ms\| ≥ ~1e17 (and `ValueError` year out of range past 9999), docstring says "refuses"; production callers pre-validate (`OptionExpiryMs` `Le(MAX_TIMESTAMP_MS)`; `options_backtest_chain.py:68`); `iv30.py:115` takes vendor ints and catches only `(ValueError, TypeError)` | 5 | robustness (P2 at most) | reproduced: `option_expiry_date(10**20)` → `OSError [Errno 84]` | fix (range guard) or leave | reproduced |
| D-5 Frontend `option-expiry-date.spec.ts:6` claims parity with `expiry.py` and pins DST-change-day literals; `test_expiry.py` pins summer/winter/half day only; values equal today | 6 | hygiene | one-sided parity | optional test | reproduced |
| D-6 two ms-per-year constants `expiry.py:31` and `american.py:105`; two ms→years conversions (`time_to_expiry_years` floors at 0, `american.years_from_ms` does not) | 7 | hygiene | | leave (`expiry` cannot import `american`: cycle via `contract`) | traced |
| D-7a `test_order_terms.py:64` `assert "50 seconds" in …RANGE_REASON` | 6 | (2) copy pin, minor | `:66` absence check stays | leave | |
| D-7b `test_structure_check.py:250` `assert name.in_words in str(...) or refusal is SAME_CONTRACT` | 6 | (2) wording pin, weakened by `or` | | leave | |
| D-7c `test_resolver.py:1222` evidence test, mostly input echo | 6 | (1) trivial in isolation | ADR d8 requires evidence; money path keep | leave | |
| D-7d `test_resolver.py:1075` name says "three reasons", asserts four | 6 | naming | | leave | |
| D-8 `whole_units.py:18` cites "ADR 0076 decision 10"; d10 is "One order, signed as Alpaca signs it"; the rule is d27 (`0076-…md:1019`) | 7 | hygiene | | fix cite | traced |
| D-9 `_THOUSANDTHS_PER_DOLLAR = 1000` in `contract.py:46`, `resolver.py:134`, `structure_check.py:37`, `at_expiry.py:56`, `watch.py:56`, `options_backtest/picking.py:62`; `100` multiplier in `resolver.py:132`, `option_effects.py:101`, `options_backtest/market.py:23` | 7 | hygiene | | leave | |
| D-10 `exact.whole_units` (`:41`) and `whole_units.whole_units` (`:68`) share a name with different meanings; `_SHAPE_REFUSALS` duplicated `option_deploy.py:507`, `option_structure_preview.py:167` | 7 | hygiene | | leave | |

Lens 3: nothing to fix in slice (every `except` re-raises with cause or becomes a typed refusal; `resolver.py:640` `LookupError` → `EXPIRY_NOT_A_SESSION`). Pointer for router reader: `app/routers/iv30.py:138-139` `except (ValueError, TypeError): continue` silently drops a contract whose expiry is off-anchor, no count. Lens 5: all expiry inputs are `OptionExpiryMs`, strikes validated at the schema; `lru_cache(1024)` bounded; pointer `options_backtest_chain.py:68` `ExpiryMs` plain bounded int guarded downstream by `expiry_date_of`. Overlap checked, not duplicates: `test_structure_check::test_every_starting_plan_comes_to_the_structure_it_is_named_for` vs `test_resolver::test_every_named_structures_starting_plan_resolves…`; `test_a_stale_quote_is_refused_as_stale` is the only STALE case at resolver level; monkeypatch of `american_implied_volatility` (`test_resolver.py:493`) is a justified seam. `docs/known-gaps.md` names no slice file. No `noqa`, `TODO`, `print`; five test `# type: ignore` are deliberate wrong-type probes.

Cross-slice pointers: `Frontend/src/app/utils/occ-ticker.ts:43` second OCC parser (Polygon `O:` form), no Python parity spec named; `app/research/options/{contract_finder,iv_builder}.py` and `app/volatility/conventions.py:88-92` carry their own `dte/365` and `/365.25`; `bot-business-manual.md:518-519` links this slice's files at blob `2a04922f`.

### E. Skipped/gated: slice 0 skipped, 0 xfail, none Postgres/network-gated, no `slow`. `test_picking_parity.py` 218 cases with vacuity guard (`:135`). `tests/services/test_option_search.py` 8 real-worker tests fail sandboxed (`PermissionError`), 8 passed unsandboxed.

### F. Runs / not read
Slice 312 (39/10/6/17/10/7/65/158); `tests/options` 928; parity + catalogue route + preview views + entry-wait + strategy engine 430; `test_option_search`/`test_watch`/`ibkr/test_option_quotes` 179 passed + 8 sandbox failures (8 passed unsandboxed). Lint extra `ARG,RUF`: 3 ARG001 (dispatch), 2 RUF002 (minus sign `expiry.py:40`, `whole_units.py:13`), cosmetic. Not done: PR #3163/#3185 diffs not read (reader's `gh` blocked), so the "waits on" rows are cross-checks for the driver; Frontend/Backend grepped only.

## R2 — Python `app/options` part 2: American pricing, at-expiry, bounds, decay, deadlines, simulated fills (read at f1288f2d; paths under `PythonDataService/` unless `docs/`/`Frontend/`)

Headline: clean. One confirmed cut (dead `step=` keyword in `bounds.py`). No P0/P1. Slice **266 passed in 20.7 s**, 0 skipped, 0 xfail; consumer run 314 passed + 8 sandbox failures (multiprocessing semaphores), `test_option_search.py` unsandboxed 60 passed. ruff (project config and lens set) clean on all 13 files.

### A. Feature table

| feature | requirement | entry | reachable | normal / empty / failure | class | evidence |
|---|---|---|---|---|---|---|
| American price + delta, Leisen-Reimer tree, escrowed cash dividends | ADR 0076 d6; engine-authority-map:97 | `american.py::american_value` | yes (resolver delta rule, preview, bot entry) | ok / n.a. / `None` when strike unresolvable, `ValueError` on invalid input | implemented | AM-001, AM-002, AM-006 (12 tests); `test_american.py` 43 |
| American IV solver with status, never a number on failure | ADR 0076 d6 | `american.py::american_implied_volatility` (`resolver.py:493`) | yes | ok / price≤0 → INVALID_INPUT / every miss a status | implemented | AM-003, AM-002/AM-006 solver tests |
| One contract's daily decay | PRD #2913; ADR 0076 d6 | `american.py::american_daily_decay` | yes via `picks_daily_decay` when `inputs.dividends is not None` (`option_structure_preview.py:1043`) | ok / `None` under a minute / refuses like the pricer | implemented | AM-004, AM-005; `test_american_daily_decay.py` 57 |
| Structure daily decay (legs summed by sign/ratio/multiplier; sold-call and near-ex-date flags) | ADR 0076 d23; map:103 | `decay.py::picks_daily_decay`, `structure_daily_decay`, `goes_ex_soon` | preview only (entry records no decay yet) | ok / leg without figure → structure none / pricer `ValueError` → NOT_PRICED | implemented | `test_decay.py` 37 |
| Figures at expiry (gain, lowest/highest results, break-evens, sketch) | ADR 0076 d17; map:101 | `at_expiry.py::at_expiry` (`preview.py:49`) | preview only | ok / mixed expiries → `None` / unbounded loss refused | implemented | `test_at_expiry.py` 21 (hand-derived, exact) |
| Number ranges read off the validating field | ADR 0076 d17, d19; map:105 | `bounds.py::Bounds`, `bounds_of`, `held_by` | catalogue, panel, preview words, order terms | ok / finite check raises `ValueError` | implemented | `test_bounds.py` 39 |
| Deadlines (close deadline, entry cutoff, sold-leg close, last session, close owed/missed) | ADR 0076 d20; ADR 0022 | `deadlines.py::*` from Clerk scheduled-close, admission, expiry, preview | yes | ok / no-session date → `LookupError` or counts back / half days correct | implemented; broker's own expiry window unverified (`deadlines.py:22-28`) | `test_deadlines.py` 42; `test_scheduled_close.py` 17 |
| Simulated fills at natural price of kept quotes | ADR 0076 d16; map:113 | `simulated_fill.py::simulated_entry_fills`, `simulated_exit_fills`, `a_price_to_close_at` | Dry Run and Live Shadow | ok / missing side → `ValueError` on entry, UNPRICED on exit | implemented as a model (real-venue claim unverifiable by design) | `test_simulated_fill.py` 15; `test_synthetic_broker_option_orders.py`, `test_simulated_multi_leg_orders.py` |

Extra: fuzzed the pricer (~1,300 random contracts twice, with/without dividends, 30 min–200 days): every SOLVED price re-prices within 1e-6, no non-finite decays; ~⅓ come back BELOW_VOLATILITY_FLOOR (flat plateau, documented `american.py:193-197`).

### B. Cut candidate

| symbol | what | proof | goes with it | conf |
|---|---|---|---|---|
| `app/options/bounds.py::bounds_of`, keyword `step` (lines 96-113: signature, docstring sentence 100-101, handling 111-112) | optional step override nobody passes | `git grep -n "bounds_of("`: 5 app call sites (`structure_catalogue.py:280`, `option_structure_panel.py:227,228`, `option_preview_out_of_range.py:134,140`) + 2 test calls (`test_bounds.py:117,133`); none passes `step=`; catalogue builds its own steps at `structure_catalogue.py:284` | nothing else; branch becomes `Decimal(1) if info.annotation is int else None`; docstring clause "Any other step is the caller's to say" goes | confirmed |

### C. Not cut

| symbol | why kept |
|---|---|
| `american.py::AmericanDailyDecay.later_price_is_exercise_value` (179, 504) | never read in app; read by `test_american_daily_decay.py:183,209,225,227,236`, `test_american_option_fixtures.py:247`, built at `test_decay.py:46`: test-only field of ◇ pricer output; those tests prove 24-hour-boundary behaviour |
| `american.py::AmericanIv.detail` (218; set at 533,535,540,565,570,575,589,592) | resolver keeps only status/volatility/delta (`resolver.py:506-512`); read by `test_american.py:107`; ◇ IV solver; owner call (D3) |
| `decay.py::LegDecay.detail` (97,130,146,151) | read by `test_decay.py:398` (D1) |
| `decay.py::StructureDecay.per_share/.per_structure/.estimated` | view reads only `total`, `why_not`, `legs_without`, sold-call tuple (`option_preview_views.py:312-354`); tests read the others; intermediate figures of documented sum — test-only production helpers |
| `deadlines.py::entry_cutoff_ms` (88-90) | no app/script caller; called at `test_deadlines.py:59,75,90,210`; resolver computes close-minus-lead inline (`resolver.py:647,1113`). Test-only production helper; cutting means turning four assertions into literals — not delete-only |
| `simulated_fill.py::SimulatedFillModel.UNPRICED` (46) | produced at 142; every caller reads `price is None`; observed by `test_simulated_fill.py:120`; money trail / safety signal "invents no fill" — Sacred |
| `golden/options-pricing/AM-001…AM-006`, `scripts/fixture_generators/american_options.py` | Sacred |

No waits-on rows, no accessibility-class.

### D. Finding rows

Lens 3:

| # | where | sev | evidence | disposition | status |
|---|---|---|---|---|---|
| D1 | `decay.py:144-146` with `option_preview_views.py:325-330` | P2 | a pricer `ValueError` (e.g. dividends ex inside the day worth the whole stock) → NOT_PRICED; preview says "the model has no price for leg N a day from now. That is usual on a contract's last full day." Refusal text kept in `LegDecay.detail`, never shown/logged. Fails closed, wrong reason | owner-ruling: surface `detail` or own `DecayNotEstimated` code | traced |
| D2 | `deadlines.py:149-161` (docstring 154-156) | robustness | `entry_cutoff_on` for a structure with a sold leg on a no-session date raises `LookupError` (Saturday and Thanksgiving reproduced); docstring names only expiring-contract case; preview guards (`preview.py:211-216`); Clerk path (`option_admission.py:211` from `runtime.py:2026`) does not, call precedes session check `runtime.py:2093`; guarded in practice by quote freshness → fails closed with an exception | leave-with-reason; fix docstring or treat as no cutoff. Overlaps #3150 | reproduced (raise), suspected (reachability) |
| D3 | `american.py:218`, `resolver.py:506-512` | hygiene | solver's explanatory text built at 8 sites and dropped by resolver; words come from `option_preview_words.py:285-292` | leave (◇) | traced |

Lens 4: nothing to fix (isolated ruff DTZ003/4/5/7, T201, E722, S110, S112, BLE001, F401, F841, ERA001, PGH003/4 clean; no hardcoded 09:30/16:00 or `mcal`; session times from `app.lean_sidecar.trading_calendar`; every provenance block names an existing running test/fixture; explicit tolerances in all float comparisons). Pointer outside slice: `Frontend/src/app/components/broker/broker-deploy-page/option-structure/option-structure.ts:436-446` re-implements `Bounds.fault` with `Number()`, server compares exact decimals (map:105); no parity test names `bounds.py` (traced). `bounds.py` has no provenance block (range check, not math).

Lens 6 (all seven files added after 2026-10-01 and read):

| # | test | kind | survivor | disposition |
|---|---|---|---|---|
| D4 | `test_decay.py:160` `test_a_sold_call_in_the_money_with_cash_going_ex…` | 3 | `test_decay.py:179` `[a cent in the money]` (580.01, asserts `("sold",)` and total 50.00, a superset) | delete |
| D5 | `test_decay.py:419` `test_three_days_is_three_times_the_day…` | 1 | `test_decay.py:412` boundary test (uses the constant) | delete or keep; views derive "3 days" from constant (`option_preview_views.py:372`) |
| D6 | `test_deadlines.py:29`, `test_decay.py:35`, `test_simulated_fill.py:20` import `tests.options.test_resolver as scenes` | hygiene | | leave; breaks if resolver cut touches `test_resolver.py` |
| D7 | `test_american_daily_decay.py:387-388` lower-bound pins (`1e-9 < gap`) | | | leave (fail loudly when known edge is fixed, comment 384-386) |
| D8 | `test_decay.py:387` monkeypatches `american_daily_decay` to raise | 4 weak | real input exists at `test_american_daily_decay.py:417` | leave |

Lens 7:

| # | where | evidence | disposition |
|---|---|---|---|
| D9 | `docs/architecture/engine-authority-map.md:122` | "no entry with a sold leg can be made through the Clerk" but `option_effects.py:213-263` takes sold multi-leg plans (simulations always; real accounts behind `multi_leg_option_orders_enabled()`) | fix sentence |
| D10 | `engine-authority-map.md:97` | lists AM-001..AM-005; manifest and `american.py:26-28` carry AM-006 | add AM-006 |
| D11 | `simulated_fill.py:22-24`, `test_simulated_fill.py:137-141` | docstring says unpriced exit is "left out of profit and loss and flagged"; test docstring says zero bid "what the books get"; both callers refuse instead (`simulated_option_orders.py:350-361`, `simulated_multi_leg_orders.py:181-188`; `exit_resolution.py:394-414` EXIT_UNPRICED) | reword docstrings |
| D12 | `decay.py:211` | `assert worked is not None` (only S101 in app code; type narrowing) | leave |

`docs/known-gaps.md` names no file here. No comment cites plan/PRD/audit/rule file; no print/console.log/noqa/type: ignore. Lens 5: pure functions, nothing.

### E. Skipped/gated: slice 0 skipped, 0 xfail, 0 Postgres/network-gated. No zero-collect parity: AM-001 400 cases, AM-003 140, AM-004 400, AM-006 6; AM-002 guarded `len == 52`, AM-005 by group-key equality; AM-001/003/004/006 have no non-empty guard of their own (manifest hash tests `tests/fixtures/test_golden_manifest.py::test_{file,content,attribution}_hashes_match_disk` 3 passed). `test_option_search.py` 8 sandbox failures (`PermissionError` creating multiprocessing semaphores), 60 passed unsandboxed.

### F. Runs / not read
`test_american` 43, `test_american_daily_decay` 57, `test_at_expiry` 21, `test_bounds` 39, `test_decay` 37, `test_deadlines` 42, `test_simulated_fill` 15, `tests/fixtures/test_american_option_fixtures.py` 12 = 266 in 20.7 s (slowest 5.2 s). Only cited lines read: `resolver.py`, `preview.py`, `option_admission.py`, `runtime.py`, `scheduled_close.py`, `option_preview_views.py`, `option_structure_preview.py`; `test_resolver.py` only `_at` + imports; ADR 0076 grepped not read in full; AM `attribution.md` and generator not read/run (needs QuantLib); tree math not re-derived (◇); `option-structure.ts` lines 425-470 only.

## R3 — Python `app/options` part 3: entry, preview, requirement, buying power, event allocation, watch (read at f1288f2d)

Headline: live and money-path-sound; no P0/P1/P2. Slice tests **362 passed, 0 skipped, 0 xfail**; fixtures/parity 62 passed; ruff lens set clean on all 14 files. Three small dead symbols (B), three test-bar rows, one robustness row. No cut waits on #3163/#3185/#3162 (diffed the three branches: none removes a caller of a symbol here).

Reachability: Dry Run and Paper take single-leg option bots with no flag; multi-leg needs `ALPACA_MULTI_LEG_OPTION_ORDERS_ENABLED` (default off, `app/config.py:298`); Live needs `ALPACA_LIVE_OPTION_BOTS_ENABLED` (default off, `:273`); judged buying power `ALPACA_OPTION_BUYING_POWER_JUDGED=False` (`:307`).

### A. Feature table

| feature | requirement | entry | reachable | normal / empty / failure | class | evidence |
|---|---|---|---|---|---|---|
| Entry preparation and refusals (`entry.py`) | ADR 0076 d2,5,7,17; #2913 | `option_admission.py:356` (Clerk); `preview.py:281` | single-leg Dry Run/Paper default; multi-leg and Live opt-in | OK; missing quote raises `ValueError` (`entry.py:205`); typed `StructureRefused`/`StructureMoneyRefused` | implemented | `test_entry.py` 22; `test_preview_and_entry_agree.py` 2 (1,920 draws, vacuity guards :188-198, :235) |
| Structure money (`requirement.py`) | ADR 0076 d2-5; Alpaca Level 3 | `entry.py:208,238,243` | same | exact whole cents; refuses unbounded loss, mixed expiry with sold leg, commitment ≤ 0, fractional cents | implemented | golden OSM-001 27 cases (manifest); `test_requirement.py` 64 |
| Held and portfolio collateral (`portfolio_broker_requirement`, `held_requirement_cents`, …) | ADR 0076 d28 | Clerk `account_picture.py`, `account_option_requirement.py`, `budget_deploy.py`, `option_position_money.py`; `options_backtest/cover_book.py:244` | Clerk paths; same gating | `{}` → `(0,0)`; `LOSS_UNBOUNDED`, `ValueError` | implemented | golden OSM-002 10; `test_cover_book_parity.py` 8 |
| Preview figures and "too late to enter" (`preview.py`) | ADR 0076 d2,5,7,16,17,23 | `option_structure_preview.py:1053`, preview route (not flag-gated) | yes | no session → None; unknown model → no delta/lean/decay | implemented | `test_preview.py` 36; services/route tests passed |
| Buying-power comparison (`buying_power_check.py`) | ADR 0076 d26; #2913, #3102 | Clerk `entry_buying_power.py:749-808`; `scripts/option_qualification_bundle.py:231` | arithmetic always; live judgment opt-in | 4 results; `rejudge` raises on damaged record | implemented (arithmetic); **unverified live** (ADR 0076:953) | `test_buying_power_check.py` 89 |
| Option-event allocation (`event_allocation.py`) | ADR 0076 d14; #2913 | `broker/contract/option_events.py:299`; Clerk `option_events.py:834` | Dry Run/Paper poll; Live opt-in | oldest-first / expiry newest-first; typed refusals | implemented | golden OEV-001 17; `test_event_allocation.py` 86 (900- and 400-draw sweeps with guards) |
| Quote-line watch and budget (`watch.py`) | ADR 0067; #2913 | `broker/ibkr/option_quotes.py:364`; `bot_option_run.py:374`; `option_deploy.py:295`; `held_option_quotes.py` | needs IBKR read-only feed | ceiling/budget → `NoLine` reasons | implemented | `test_watch.py` 63 |
| Entry-outcome words (`entry_came_to.py`) | ADR 0076 | `bot_option_run.py:159-169`, `bot_dry_run.py:115` | Dry Run/Paper | closed set; every `EntryStanding` mapped | implemented | `test_option_bot_entry_outcome.py:1545` |

### B. Cut candidates

| symbol (lines) | what | proof | goes with it | conf |
|---|---|---|---|---|
| `app/options/event_allocation.py::EVENT_CLOSE_PRICE` (101-102; comment :213) | `Decimal(0)` constant | `git grep -w EVENT_CLOSE_PRICE`: only `event_allocation.py:102,213`, `tests/options/test_event_allocation.py:23,116`; production writes literal `Decimal(0)` (`clerk/sqlite/option_events.py:1747,2450`); only reader is `assert Decimal(0) == EVENT_CLOSE_PRICE` | test import :23, assertion :116; reword :213 comment | confirmed |
| `app/options/preview.py::PickedLeg.multiplier` (:99; set :177) | per-leg multiplier on preview record | no production reader (`option_preview_views.py` reads `size.multiplier` :190,:446; `leg.multiplier` nowhere in `app/`; no `asdict`/`vars`); only `test_preview.py:128` | `test_preview.py:128`; `tests/services/test_option_preview_views.py:197` kwarg | confirmed |
| `app/options/entry.py::LegGapOverLimit` attrs `contract`, `bid`, `ask`, `limit_percent`, property `gap_percent_of_mid` (:86-95) | exception payload "a screen needs" | production reads only `.leg_id` (`preview.py:190`) and `str(exc)`/`.reason` (`option_admission.py:366-367`); only `test_entry.py:156-159,200` read the rest; `bid_ask_gap_percent_of_mid` itself stays (`preview.py:180`) | `test_entry.py:156-159,200`, then-unused `Fraction` import (:7); keep `__init__` params (message uses them) | probable |

### C. Not cut

- `requirement.py::broker_requirement` (:197): no `app/` caller (`entry_money` inlines it :377) but pinned by golden OSM-001 (`test_option_structure_money_fixture.py:64,:73`) and `test_cover_book_parity.py:262` — Sacred.
- `event_allocation.py::OptionEventAllocation.share_price` — read by golden OEV-001 test.
- `OptionEventAllocation.{kind, contract, contracts, multiplier, share_quantity_change, cash_change_cents}`, `LotAllocation.remaining_quantity` — test-only readers proving allocation invariants that stay (`test_event_allocation.py:649-665`, `test_option_events.py:390,573`): test-only production helper.
- `watch.py::LinePlan.lines` — tests assert priority order via `.lines` (`test_watch.py:261,270,293,318`): test-only helper.
- `buying_power_check.py::ComparisonRecord` — `rejudge` annotation. `JUDGMENT_NOT_SWITCHED_ON_REASON` and every `buying_power_check`/`requirement`/`watch`/`entry_came_to` symbol: live callers confirmed.
- All `StructureMoneyRefusal`, `NoLine`, `LineClass`, `OptionEventRefusal` members raised or consumed (Frontend `broker.types.ts` carries `OPTION_EVENT_*`).
- `Frontend/src/assets/docs/bot-business-manual.md:520` link pinned to SHA `2a04922f` — pointer only. No waits-on rows; no accessibility-class.

### D. Finding rows

Lens 3: nothing to fix — all gates fail closed (`entry.py:205` missing bid/ask raises; `:234-242` missing fee rate gives no money and preview names `FEE_RATE_NOT_PINNED` `preview.py:227`; `requirement.py:321-330` refuses crossed/zero-ask/NaN/float; `buying_power_check.py:209-229`; `watch.py:261-265` every wanted-but-unheld contract has a `NoLine` reason; `options/quotes.py:87-101` `quote_fault` upstream; `preview._too_late_to_enter` treats no-session date as "not too late" `:211`, advisory — the Clerk is the gate).

| item | lens | sev | evidence | disposition | status |
|---|---|---|---|---|---|
| `event_allocation.py::allocate_option_event` `event_date_ms` unchecked | 4/5 | robustness | `event_date_ms` (:355, used :434) skips `_require_an_instant` (:148) though `HeldOptionLot.opened_at_ms` gets it (:184); float `1689900000000.5` allocated; `True` and `-5` refused with misleading "assigned on 1969-12-31"; callers pass broker-derived ints | fix (one line) or leave | reproduced |
| time/numerics otherwise | 4/5 | none | no `time(9,30)`, naive datetime, second `get_calendar`; money math `int`/`Decimal`/`Fraction`; goldens OSM-001/OSM-002/OEV-001 in manifest, collect 27/10/17 | — | traced |

Lens 6 (not part of cut):

| test | kind | evidence | survivor | disposition |
|---|---|---|---|---|
| `test_buying_power_check.py:243-244` `test_the_reason_the_clerk_writes_…` | 1/2 | `entry_buying_power.py:137` defines `JUDGMENT_NOT_SWITCHED_ON = JUDGMENT_NOT_SWITCHED_ON_REASON` so it asserts `X.value == X`; wire value pinned by `test_entry_buying_power.py:371`, `test_option_qualification_bundle.py:125` | those two | delete defensible; owner's call |
| `test_entry.py:127-140` `test_each_leg_of_that_structure_is_inside_the_gap_limit…` | 3 | same quotes/8% limit as :110-124; both assert `reason is COST_OVER_LIMIT`; `net_limit_price == 0.14` implied by :121 | `test_a_structure_that_costs_most_of_itself_to_trade_is_refused_by_the_cost_limit` (:110-124) | delete |
| `test_preview.py:110-113` `test_a_cost_is_a_percent_of_what_is_set_aside_and_exact` | 3 | `percent_of(800,1400)==Fraction(400,7)` asserted again :144; `whole <= 0` guard (`preview.py:86`) unreachable from production (`requirement.py:380`) | `test_a_cost_over_the_limit_is_said_and_stops_nothing` (:137-145) | delete test; leave guard |

No mock theater or tombstones. 9 deliberate `# type: ignore[arg-type]` in `test_buying_power_check.py` and `test_requirement.py` (bad-type boundary tests).

Lens 7: two private `_require_whole` helpers with opposite argument order (`buying_power_check.py:245` `(value, name, *, least)` vs `event_allocation.py:143` `(name, value, *, at_least)`; bool-or-not-int check repeats at 16 sites in `app/options`) — leave. `LegGapOverLimit` docstring "Carries what a screen needs" (`entry.py:76`) folds into B3. Comments cite ADRs/issues only; `known-gaps.md` names no file here. Pointer: `tests/options/test_resolver.py` doubles as scene library for 4 tests here (`from tests.options import test_resolver as scenes`). Pointer: ADR 0076 §3 owner question already recorded (bought-only structures priced under their expiry value are admitted).

### E. Skipped/gated: none; no zero-collect parity (OSM-001 27, OSM-002 10, OEV-001 17, cover-book 8). Outside slice: `tests/services/test_option_structure_preview.py::test_the_event_loop_keeps_running_while_a_real_worker_process_searches_for_an_iron_condor` fails sandboxed (`PermissionError` from `os.sysconf("SC_SEM_NSEMS_MAX")`, ProcessPoolExecutor), passes unsandboxed.

### F. Runs / not read
Per file: buying_power_check 89, entry 22, event_allocation 86, preview 36, preview_and_entry_agree 2, requirement 64, watch 63 = 362; fixtures+parity 62; cross-slice callers 400 (+1 sandbox failure, passes unsandboxed) and 324. Skimmed: `entry_buying_power.py` (85-140, 735-830), `option_admission.py` (223-420), `option_preview_views.py`, `option_structure_preview.py`, `option_quotes.py`, `held_option_quotes.py` (grep only). Not read: other `app/options` files (other readers), `test_resolver.py` internals, fixture generators, Backend/, Frontend (grep only).

## R4 — Python volatility package and its math fixtures (read at f1288f2d; Python paths under `PythonDataService/`)

Headline: no P0/P1. Everything else P2, robustness or hygiene; nothing Sacred is dead. Runs: `tests/volatility` + `tests/edge/test_iv30_and_vrp.py` + `test_iv30_stability.py` **207 passed, 13.0 s**, 0 skipped, 0 xfail; `test_volatility_fixtures.py` 9 + `test_ibkr_iv_fixtures.py` 2 + `test_golden_manifest.py` 10 = **21 passed**; `ruff check` clean. Repros in the session scratchpad (`r4_repro.py`, `r4_repro2.py`, `r4_repro3.py`, `r4_sabr.py`, `r4_arb.py`).

Direct answers: **Q2** parity tests collect: IV-001 12, IV-002 21, IV-003 3, IV-004 1, RV-001 1, RV-002 20, RV-003 3, RV-004 3, OPT-IB-002 2,332 rows; no empty parametrize, none zero-case. **Q3** no `np.allclose`/`np.isclose`; 32 `pytest.approx(...)` at default `rel=1e-6` (22 `test_price_normalization.py`, 9 `test_vix_replication.py`, 1 `test_iv30_stability.py`); 3 `assert_allclose(..., atol=...)` leave `rtol` default (`tests/edge/test_iv30_and_vrp.py:95,122,136`); golden tests take explicit tolerances from manifest. **Q4** the `0.05` default no longer exists in the volatility package (only docstring example `surface.py:186`; test-only `tests/volatility/conftest.py:24`); `SurfaceConventions.rate` is `kw_only` with no default (`conventions.py:33`); `DEFAULT_RISK_FREE_RATE = 0.043` at `app/services/risk_free_rate.py:26` is the single Python default (#2764 commits `a6aa280a`, `e426fc23`, `61fd8a6a`); #2747 item closed on Python side. **Q5** clean in `app/volatility` (no utcnow/naive datetime/16:00/09:30/fixed offset; `basis.py:54` `America/New_York` by name, `basis.py:35` canonical `trading_session_count`; ACT/365↔TRD/252 applied once at `routers/edge.py:158,422`). **Cut leftovers verified gone:** `app/volatility/` holds only the 11 `.py` files; `cache.py`, `data_loader.py`, `models.py`, `example.py`, `docs/`, `tests/volatility/test_cache.py` deleted in #2755 (`38474712`); only live reference is ADR 0049:53 (D-7.7).

### A. Feature table

| feature | requirement | entry | reachable | normal / empty / failure | class | evidence |
|---|---|---|---|---|---|---|
| IV solver cascade (Newton → QuantLib → Brent) | ADR 0071 §1 | `solver.py::implied_volatility` ← `routers/iv30.py`, `services/options_companion_service.py`, `research/options/iv_builder.py` | yes (`main.py:1413`) | `price<0.001` → `PRICE_TOO_LOW`; sub-minute ttm → `EXPIRED`; NaN → `CONVERGENCE_FAILURE` (D-5.2) | implemented | IV-001 (atol 1e-6), OPT-IB-002, `test_solver_parity_pyvollib.py` (1,043 cases, max diff 2.7e-6), `test_solver.py` 30 |
| Live VIX-style IV30 route | ADR 0071 §3 | `routers/iv30.py` → `vix_style_iv30_with_provenance` | yes; Frontend edge-api calls `/api/edge/iv30/vix-style` | math verified; route 400/502 other reader's | implemented (math) / unverified (route) | `test_vix_replication.py` 25; production path has no independent golden (#2704 addendum "a gap to note") |
| Parametric IV30 | ADR 0071 §3 | `routers/iv30.py` → `solver` + `iv30_atm_50d` | yes | | unverified | `iv30_atm_50d` hand tests; router not read |
| IV recorder snapshot (opt-in) | ADR 0071 §1–9 | `services/iv_recorder.py:323` → `compute_iv30_health_normalized` | opt-in `IvRecorder:Enabled=false`; not dead | writes error rows on replication failure; `iv30_parametric` always `None` ("Owed", ADR 0071 §3) | partial (by ADR) | `test_iv30_stability.py::TestNormalizedHealthParity` |
| IV30 health score | ADR 0071 | `iv30_health.py` | via recorder | D-3.6 | implemented | `test_iv30_stability.py` 11 (abs 1e-9 legacy vs normalized) |
| IV ACT/365 ↔ TRD/252 basis | `docs/references/iv-rv-basis-alignment.md` | `basis.py` ← `routers/edge.py:422` | yes | `ValueError` on tenor ≤0, σ<0, empty window | implemented | RV-003 (atol 1e-9), `test_basis.py` 15 |
| Price normalization + provenance schema | ADR 0071 §4–5 | `price_normalization.py`, `iv_provenance.py` | yes | contract `ValueError`s | implemented | `test_price_normalization.py` 28, `test_iv_provenance.py` 7 |
| Surface build/fit/analytics (SABR/SVI/variance, skew, health) | ◇; routes cut #2755 | `surface.py`, `fitting.py`, `analytics.py` | **no production entry**, tests only by ruling | arbitrage check fails open (D-3.1) | implemented, unreachable by design | `test_surface.py` 14, `test_fitting.py` 14, `test_analytics.py` 24; IV-002 covers `fit_svi` only |
| Conventions (`SurfaceConventions`, day-count) | ◇ (`discount_factor` named) | `conventions.py` | constants used by `basis.py`; rest test-only | | implemented | `test_conventions.py` 23 |
| Legacy IV30 (`vix_style_iv30`, `OptionQuote`, `compute_iv30_health`) | ◇ | scripts, tests | test/generator/script only | | implemented | IV-003, RV-004 (independent pure-Python oracle, atol 1e-6), stability tests |
| Golden fixtures IV-001..004, RV-001..004, OPT-IB-002 | numerical-rigor | `tests/fixtures/test_volatility_fixtures.py`, `test_ibkr_iv_fixtures.py` | yes | all load and run | implemented | 21 passed |
| iv30 SPY 2024-12-20 snapshot anchor | `golden/README.md` | `test_vix_replication.py::TestSpyGoldenFixture*` | yes (`skipif` absent) | core assertion self-referential (D-4.8) | partial | 4 tests |

Per-symbol callers (Q1): `solve_iv_chain`, `convert_iv_trading252_to_act365`, `compute_put_call_parity_forward`, `fit_svi`, `fit_sabr`, `fit_variance_interp`, `check_smile_arbitrage`, `VolSurface*`, `find_delta_strike`, `compute_skew_metrics`, `compute_health_score`, `replicate_expiry_variance`, `vix_style_iv30`, `wrap_legacy_as_opra_mid`, `from_recorded_snapshot`, `from_eod_close*`, `tiered_moneyness_half_spread`, legacy `compute_iv30_health`, `iv30_atm_50d_trading_basis`, `skew_25d`, `term_slope`, `iv_change`, `iv_vol`: **no production callers; tests only**. Receipts: hand (`solve_iv_chain` tol 1e-3, basis round-trip abs 1e-12, parity-forward abs 1e-9 analytic BSM forward), golden (`fit_svi` IV-002, `vix_style_iv30` IV-003/RV-004), none ("Validated against: NONE": `fit_sabr`, `fit_variance_interp`, `check_smile_arbitrage`, `VolSurface*`, `find_delta_strike`, `compute_skew_metrics`, `compute_health_score`); `wrap_legacy_as_opra_mid` and `iv30_atm_50d_trading_basis` have **no test**.

### B. Cut candidates

| # | symbol | what | proof | goes with it | conf |
|---|---|---|---|---|---|
| B1 | `solver.py:51` `SolveStatus.OK = "ok"` | status member nothing returns | `git grep -w "SolveStatus.OK"`: one hit `tests/fixtures/test_ibkr_iv_fixtures.py:54`; `implied_volatility` returns only NEWTON_OK/QUANTLIB_OK/BRENT_FALLBACK + error statuses | the `SolveStatus.OK,` line in `_OK_STATUSES` (test file :54) | confirmed |
| B2 | `conventions.py:60-73` `SurfaceConventions.to_hash_dict` (+ "surface_id hash" wording; `Any` at :15 becomes unused) | hash-key helper for the cut `cache.py`/`models.py` surface id | `git grep -w to_hash_dict`: `conventions.py` and `tests/volatility/test_conventions.py` only; `surface_id` exists nowhere else | `test_conventions.py::TestSurfaceConventionsDataclass` (2 echo tests ~88-114); `forward`/`discount_factor` and fields stay | confirmed callers / probable on owner view |
| B3 | `surface.py:346-361` `VolSurfaceBuilder.build_bid_ask` | builds bid/ask surfaces | `git grep -w build_bid_ask`: only the definition; no test | nothing | confirmed no caller; probable on keep (file kept whole by ◇; untested plumbing not validated math) |
| B4 | `surface.py:90,196,203,302,341` `eval_date: str` on `VolSurface` and `VolSurfaceBuilder` | ISO-string field assigned and passed, never read | `git grep -nE "\.eval_date"`: no reader in package (`solver.py` hits are an unrelated QuantLib local) | the `VolSurface(...)` keyword at two builder return sites | confirmed |
| B5 | `app/volatility/__init__.py:17-30` re-export block and `__all__` | package-level aliases for 8 names | `git grep -E "from app.volatility import|import app.volatility$"`: nothing; every consumer imports a submodule | docstring also stale (D-7.3); side effect: `import app.volatility.basis` stops importing QuantLib/scipy/pandas through the package | confirmed no importers; probable on taste |

### C. Not cut

- `wrap_legacy_as_opra_mid` (`vix_replication.py:171`): no caller, no test; kept by #2704 ◇ addendum.
- `from_recorded_snapshot`, `from_eod_close`, `from_eod_close_tiered_moneyness`, `tiered_moneyness_half_spread`, `DEFAULT_HALF_SPREAD_RULE`, `TIERED_MONEYNESS_HALF_SPREAD_RULE`: test-only; "the `price_normalization` functions" named in ◇.
- `solve_iv_chain`, `compute_put_call_parity_forward`, `convert_iv_trading252_to_act365`, `SurfaceConventions.forward`/`discount_factor`; legacy `vix_style_iv30`, `replicate_expiry_variance`, `_select_atm_strike`, `OptionQuote`, `compute_iv30_health` and helpers; `analytics.py`/`surface.py`/`fitting.py` as math; `iv30_constructor.py` incl. `iv30_atm_50d_trading_basis`: named in the addendum.
- `dte_to_ttm`, `ttm_to_dte`, `Actual360`/`ActualActual` branches (15 tests): test-only; production hand-rolls `/365` (`vix_replication.py:494`, `analytics.py:189`); ◇ scope is conventions arithmetic with hand-calculated tests — **owner ruling if you want it cut**.
- IV-001..004, RV-001..004, OPT-IB-002, iv30 snapshot, generators, `build_iv30_golden.py`: Sacred. `FitMethod` vs `SurfaceMethod` identical enums both used. `replicate_expiry_variance_with_provenance` duplicating the legacy walk declared deliberate (`vix_replication.py:235-238`).
- Cross-module pointers: `app/services/quantlib_pricer.py:305` `implied_volatility` second IV solver, no caller, no test, uses `date.today()` (solver.py:7-8 names it "companion", parity "pending-fixture"); `app/research/options/iv_builder.py:25-27` redefines `MIN_OPTION_PRICE=0.05`, `MIN_IV=0.05`, `MAX_IV=3.0` vs `solver.py:37-47` 0.001/0.005/5.0; `app/routers/edge.py:156-160` `.ffill()` on IV/RV conflicts with comment `:142-144`; `delta_inversion.py` test-only (◇); hand-typed 252 literals (`hf_realized_vol.py:146`, `hf_forward_rv.py:52`) beside `conventions.TRADING_DAYS_PER_YEAR`.

### D. Finding rows

Lens 1: **D-1.1** `iv30_constructor.py:73-89 iv30_atm_50d_trading_basis` — no caller, no test; `edge.py:413-423` converts per timestamp itself; leave (Sacred), add a test when touched (traced).

Lens 3:

| ID | symbol | sev | evidence | disposition | basis |
|---|---|---|---|---|---|
| D-3.1 | `fitting.py:369-375,396` `check_smile_arbitrage` | P2 (no production caller) | a fit whose vol raises at every strike (all-NaN or `_vol_fn=None`) returns `passed=True`, `total_checks=5`; NaN appended then never counted | fix (count unevaluable, `passed=False`) | **reproduced** |
| D-3.1b | same `:385-390` | P2 | shuffled W-shaped smile → 0 butterfly violations (sorted: 1); triples with non-ascending strikes skipped; `surface.py:268` does not sort | fix (sort in checker) | **reproduced** |
| D-3.2 | `fitting.py:381-382` `negative_variance` | robustness | `w = v*v*ttm` never negative, `FitResult.volatility` raises for `v<=0` first; counter cannot fire; `test_no_negative_variance`, `test_flat_smile_passes` assert 0 vacuously | owner ruling build/drop | traced |
| D-3.3 | `fitting.py:344`, `analytics.py:288-296` `calendar_violations` | robustness | never assigned in `app/`; "−15 per calendar violation" term (`analytics.py:296`) always 0 | owner ruling build/drop | traced |
| D-3.4 | `analytics.py:212-213` `compute_skew_metrics` | P2 | vol failure at forward±1% → `skew_slope = 0.0`, same as flat skew | fix (None/raise) | **reproduced** |
| D-3.5 | `test_analytics.py:147-148,156-158,179-182,253-254,262-263` | robustness | `if result is not None: assert ...` in five tests; all converge today so they bite | leave | **reproduced** |
| D-3.5b | `test_solver_parity_pyvollib.py:105-108,118-119` | robustness | `except Exception: continue` (S112/BLE001) + `if ours_res.iv is None: continue`; 15 of 1,058 comparable cases return `PRICE_TOO_LOW` while py_vollib solves them, skipped silently, bounded only by `cases > 100` | fix (assert skip set) | **reproduced** |
| D-3.6 | `iv30_health.py:52,128` | robustness | `return keep if len(keep) >= 5 else quotes` returns full chain → `delta_resample = 0`, score 1.0 on tiny chains; `_drop_alternates` on 5-strike chain raises "need at least 3", recorder logs and `health_score=None` | leave | traced |
| D-3.7 | `vix_replication.py:55-61` vs `price_normalization.py:154-163` | P2 (production uses normalized path) | legacy `call_mid` for bid=0/ask=0.5 gives 0.25, normalized 0 (`is_zero_bid`); crossed 0.6/0.5: legacy 0.55, normalized 0; differ at ATM selection and K0; `from_snapshot_quote` docstring (:151-153) claims it "matches the legacy … semantics"; IV-003 uses `bid==ask` so cannot see | fix docstring or align | **reproduced** |
| D-3.8 | `surface.py:317-319` | hygiene | `SliceDiagnostics.n_failed=0` hard-coded while `failed` counted at `:251,271` | fix | traced |
| D-3.9 | `iv_provenance.py:75-79`, `vix_replication.py:411-414`, `services/iv_recorder.py:303-311` | P2 (log only) | docstring says pre-drop share "goes to the threshold-event log"; replicator overwrites; recorder passes `prov.max_single_strike_share` as both `max_share_before` and `max_share_after`, hard-codes `strikes_remaining=0` | fix | traced |
| D-3.10 | `test_vix_replication.py:189-192,840-843`, `test_ibkr_iv_fixtures.py:44-48` | robustness | a missing fixture file skips the class silently; iv30 parquet not manifest-governed, nothing else asserts it exists | fix with D-4.8 | traced |

Lens 4:

| ID | symbol | sev | evidence | disposition | basis |
|---|---|---|---|---|---|
| D-4.1 | `scripts/fixture_generators/volatility.py:128` | hygiene | `mcal.get_calendar("NYSE")` second calendar construction (ban list); docstring "same logic as canonical"; only other is `app/lean_sidecar/trading_calendar.py:29` | owner ruling (independent oracle is a legitimate reason; state it) | traced |
| D-4.2 | `solver.py:7-8`, `basis.py:6`, `vix_replication.py:6`, `fitting.py:8`, `surface.py:8` | hygiene | provenance blocks say `NONE`/"pending per registry"; existing receipts: RV-003 `basis`; IV-001, OPT-IB-002, py_vollib parity for `solver`; IV-003, RV-004 for `vix_replication`; IV-002 for `fit_svi`; "per registry" cites registry cut in #2750; solver's companion-parity claim still true | fix (cite receipts) | traced |
| D-4.3 | `analytics.py:61-89 _bs_delta`, `solver.py:323-325,356-364` | hygiene | delta and BS-price copies beside canonical `app/services/bs_greeks.py`; no parity test names `bs_greeks` for `_bs_delta`; solver copies covered by round-trip | owner ruling (Sacred file); delegation + named parity test | traced |
| D-4.4 | `fitting.py:119-120` `_sabr_vol` | P3 | `if abs(x_z) < eps: x_z = 1.0` makes `z/x_z = z`, not limit 1; at \|F−K\|≈1e-10 vol is −0.0 (then raises, D-3.1 swallows) | fix `ratio = 1.0 if abs(x_z) < eps else z/x_z` | **reproduced** |
| D-4.5 | 32 default-tol `approx`, 3 `assert_allclose` without `rtol` | hygiene | all compare analytic/structurally exact values | fix (add `abs=`/`rel=`) | traced |
| D-4.6 | OPT-IB-002 `test_ibkr_iv_fixtures.py:115,132-134`, manifest, `attribution.md` | robustness | both tests discard `_out`; `oracle_ibkr_iv` (only output column) read by no test since "range-plausibility test" cut in #2759; manifest note "1e-3 (0.1 vol)" but `atol` applied to dollar price (`:189`); receipt proves price-space inversion, not IBKR-IV parity | fix docs; owner ruling on whether IV parity wanted | traced |
| D-4.7 | `basis.py:70` | P3 | `isinstance(asof, int)` False for `np.int64` → loud `AttributeError ... 'tzinfo'`; docstring `:67` "naive treated as UTC" but code (:44-54) treats naive as ET date | fix | **reproduced** |
| D-4.8 | iv30 snapshot | hygiene | not in `manifest.json` (README:148-153 documents as deferred; `known-gaps.md:70-71`); deterministic test compares `vix_style_iv30` to a value written by `build_iv30_golden.py` using the same function (self-referential); `test_vix_replication_in_realistic_band`, `test_skew_premium_below_300bps` re-read `meta.json` only. **Fix:** register an id (`source: polygon`, `reference.kind: internal_regression`), `validated_by: tests/volatility/test_vix_replication.py`, drop `skipif`, assert existence; README's "needs Arrow IPC" blocker does not hold (`golden_support/hashing.py:82-90` hashes `.parquet`/`.meta.json` as raw bytes, `.json` canonically); for a real receipt add an independent oracle (CBOE comparison is a ±50 bp band, not equivalence) | fix | traced |

Lens 5: **D-5.1** `fitting.py:57-59` `SmileSlice.__post_init__` validates by `assert` (stripped under `-O`); builder passes unsorted strikes (`surface.py:268`) — fix (robustness, traced). **D-5.2** `solver.py:164` `spot <= 0` guards pass NaN; NaN price/spot/ttm/rate/strike → `CONVERGENCE_FAILURE` not `INPUT_ERROR`; NaN ttm logs "Unexpected QuantLib error" — fix (P3, **reproduced**). **D-5.3** `solver.py:242` sets global `ql.Settings.evaluationDate` per call; single-threaded today — leave (suspected).

Lens 6 (no test in slice added after 2026-10-01; last commits are cuts; findings only): `test_surface.py:99-113` interpolation asserts `iv > 0` only (comment promises between-neighbours); `test_iv30_stability.py::TestNormalizedHealthTargetTenor::test_target_tenor_changes_baseline_score` asserts only `[0,1]`, subsumed by `test_target_outside_bracket_raises`; `test_vix_replication.py::test_max_single_strike_share_in_unit_interval` tautological; `test_vix_replication_in_realistic_band` and `test_skew_premium_below_300bps` oracle-only meta re-reads (#2744 reading: "go like anywhere else"); `test_analytics.py::test_health_score_good_surface` asserts `> 0`; `test_iv30_and_vrp.py:101-102` asserts `iv30 is not None` after the comparison; `bs_price` defined three times (`conftest.py:34`, `test_solver.py:19`, `test_surface.py:16`). Whole slice runs in 13 s; no `slow` marks needed; no flakes in one run.

Lens 7:

| ID | where | evidence |
|---|---|---|
| D-7.1 | `golden/options-pricing/OPT-IB-002/v1/attribution.md:23`, `scripts/fixture_generators/ibkr_iv.py:136`, `docs/references/bouchaud-farmer-lillo-2008-market-impact.md:41` | cite `docs/math-sources-of-truth.md`, cut in #2750; IV/RV `attribution.md` not in `file_sha256`, editing breaks no hash; generator embeds same text, edit both |
| D-7.2 | `golden/iv30/attribution.md:11`, `golden/README.md:150`, `scripts/build_iv30_golden.py:18` | names nonexistent `scripts/capture_iv30_snapshot.py` and a `conftest` capture; README lists 4 consuming tests, only `test_vix_replication.py` reads the file; script docstring "within 50 bps", test bound 300 bps |
| D-7.3 | `volatility/__init__.py:1-13`, `fitting.py:5,12,15,17` | both claim QuantLib `BlackVarianceSurface`/`SabrSmileSection`/`SviSmileSection`; `fitting.py` imports only numpy and scipy |
| D-7.4 | `docs/references/iv-rv-basis-alignment.md:60-90` | roadmap sections ("Step 1 of 8", "Wiring deferred … bug still live", "What this does NOT fix") stale: `edge.py:158` converts, rate/dividend 0.0 fixed #2764 |
| D-7.5 | `solver.py:156` | "Default is the module floor (1 calendar day)"; constant is 1 minute (`:46`) |
| D-7.6 | `iv30_health.py:1,8`, `iv_provenance.py:21,54`, `test_basis.py:1`, `test_solver_parity_pyvollib.py:1`, `test_vix_replication.py:1`, `test_iv30_stability.py:1,~290`, `test_iv_provenance.py:1` | comments cite "Step N of IV-RV alignment", "locked plan", "Round 3 issue #2", "The plan"; AGENTS.md allows only ADR and issue numbers |
| D-7.7 | `docs/architecture/adrs/0049-…md:53` | names deleted `app/volatility/data_loader.py`; point-in-time list; leave or amend |
| D-7.8 | `requirements-light.txt:17-19` | `py_vollib==1.0.1` (GPL; `golden/README.md:33-39` says tests/generators only) in runtime-image layer (`Dockerfile:25-26`); belongs in `requirements-dev.txt`; owner ruling (container pytest would lose it) |
| D-7.9 | `test_ibkr_iv_fixtures.py:18-23`, `test_vix_replication.py:5-8` | "Skips if … status=planned", "once populated" stale; manifest status `active`, files exist |
| D-7.10 | `analytics.py:264` | `else 0.05` unreachable |
| D-7.11 | `docs/known-gaps.md:70-71` | iv30 entry accurate and still open; no closed entry for this slice |

### E. Skipped/gated
None skipped/xfail in my runs. `test_vix_replication.py:189` (`TestSpyGoldenFixture`, 3 tests) and `:840` (`TestSpyGoldenFixtureWithProvenance`, 1): `skipif(not GOLDEN_FIXTURE.exists())` — present, ran; removal would skip silently (D-3.10). `test_ibkr_iv_fixtures.py:44` module `skipif(not registry.exists_on_disk("OPT-IB-002"))` — ran 2; manifest `test_content_hashes_match_disk` skips missing files (`test_golden_manifest.py:165-168`). Dynamic skips present, not fired: `test_solver.py:91`, `test_surface.py:103`. No Postgres/network gating. No zero-case parity; `test_iv_agrees_with_py_vollib_within_5bps` 1,043 cases (grid 1,440, 382 vega-skipped, 15 `PRICE_TOO_LOW`).

### F. Not read
`tests/routers/test_iv30_router.py`, `tests/services/test_iv_recorder.py`, `app/routers/iv_recorder.py`, `app/routers/quantlib_options.py` (other readers; read only `iv30.py:100-200`, `iv_recorder.py:255-419`). Skimmed: `options_companion_service.py` and `iv_builder.py` (grep), `edge.py:140-215,405-423`, `test_price_normalization.py` (headers + #2759 diff), `scripts/fixture_generators/volatility.py` (1-200 + grep), IV/RV `attribution.md` other than OPT-IB-002. Not audited: oracles for RV-001/RV-002/IV-004 (`realized_vol.py`, `hf_realized_vol.py`, `options_features.py`), `delta_inversion.py` beyond callers. Unverified: "published VIX closed ≈17.5% on 2024-12-20" (`test_vix_replication.py:201`, `golden/iv30/attribution.md`, `iv-ownership-research.md §6.3`). Frontend/Backend: greps only.

## R5 — the four routers and the services behind them (read at f1288f2d; Python paths under `PythonDataService/`)

Corrections to the brief: `options.py` exposes `POST /api/options/expirations` (`options.py:17`), not GET; the stale comment at `main.py:1414` names a `GET .../series/{ticker}` that does not exist — `iv_recorder.py` has one route, `POST /snapshot`.

Headline: (1) the live IV30 routes and the recorder never receive a multi-expiry chain (the Polygon client defaults the snapshot to today's expiry and the routers drop 0-day expiries); (2) `POST /api/options/expirations` returns the Polygon API key in its 500 body when Polygon answers 4xx (reproduced offline); (3) `/api/quantlib/compare` silently drops engines and reports zero Greeks while returning `success: true`.

Runs (host venv; all 0 skipped, 0 errors): `test_iv30_router.py` 7, `test_iv_recorder.py` 9, `test_default_risk_free_rate.py` 4, `test_bs_greeks.py` 38, `test_bs_cross_engine_parity.py` 361, `test_rate_dividend_service.py` 5, `test_options_companion_service.py` 37, `test_edge_recorder_fallback.py` 18, `tests/edge/test_iv30_and_vrp.py` 13, `test_iv30_stability.py` 11, `tests/test_fred_service.py` 13, `tests/test_snapshot.py` 6, `tests/fixtures/test_options_pricing_fixtures.py` 3 + `_extended.py` 4, `tests/volatility/test_solver_parity_pyvollib.py` 3; Frontend `quantlib.service.spec.ts` + `edge-api.service.spec.ts` 5; `export_openapi_contract.py --check` exit 0; `scripts/check_temporal_authority.py` passed; ruff project config passed. .NET not run (no dotnet). Scratch probes `r5_*.py` in the session scratchpad.

### A. Feature table

| feature | requirement | entry | reachable | normal / empty / failure | class | evidence |
|---|---|---|---|---|---|---|
| Option expirations list | ADR 0022 (i); Pricing Lab, Strategy Builder, Options Lab chain | `options.py:17`; Backend `PolygonService.cs:272`, `Query.cs:80`; `market-data.service.ts:253` | yes | ok / `[]` / 500 or silently partial (D-3, D-3b, D-4, D-5) | **unverified** (no test hits the route or `list_options_expirations`) | offline probes only; `QueryTests.cs:107` mocks the service |
| QuantLib `/price` | #2764; broker-options-chain engine column | `quantlib_options.py:72`; Backend `PolygonService.cs:331`; `quantlib.service.ts:66`; `broker-options-chain.component.ts:380` | yes (`app.routes.ts:523`) | `analytic_bs` ok; non-analytic engines no test; failure HTTP 200 `success:false` zeros | **implemented** for `analytic_bs`; **unverified** for other 5 engines | `test_default_risk_free_rate.py:95`; `test_bs_cross_engine_parity.py` 361 |
| QuantLib `/compare` | Pricing Lab; ADR 0022 amend. 2026-10-09 | `quantlib_options.py:434`; Backend `PolygonService.cs:382`; `market-data.service.ts:354` | yes (`app.routes.ts:134`) | 7 curves / bad spot range: engines silently dropped / expired `success:false` | **partial** (D-6..D-9); engine curves untested | `test_default_risk_free_rate.py:112` sets `_QL_AVAILABLE=False`, so only `python_bs` tested |
| Live IV30 (`/vix-style`, `/parametric`) | ADR 0071; Step C | `iv30.py:186`, `:243`; `edge-api.service.ts:149-152` | route yes; Frontend marker yes | mocked chain ok (7 tests); real Polygon 502 "got 0" (D-1) | **partial / broken against the real client** | `test_iv30_router.py` mocks at the client boundary |
| IV recorder (Quartz cron, route, JSONL store, edge fallback read) | ADR 0071 d6-d9 | `iv_recorder.py:67`; Backend `IvRecorderJob.cs:44`; read by `edge.py:258` | **opt-in**: `IvRecorder:Enabled=true` (config or env `IvRecorder__Enabled`, `Backend/appsettings.json:16`); not set in `compose.yaml` | mocked writes rows; real error rows (D-1); store not on a volume (D-10) | **unverified** (opt-in, not dead) | `test_iv_recorder.py` 9; `test_edge_recorder_fallback.py` 18. Driver: the running `polygon-data-service` has no `/var/lib/iv-recorder` (recorder not enabled there) |
| Options companion export | `docs/options-companion-format.md`; ADR 0076 | `dataset.py:326`; `options_companion_service.py:452` | yes (Data Lab export) | helpers tested; Polygon failures reported as "no listed expiry" (D-12) | implemented (math/format), unverified (live fetch) | `test_options_companion_service.py` 37 |
| One default risk-free rate (#2764) | #2764 | `risk_free_rate.py:26` | yes | omitted = explicit default; failed FRED = same number | **implemented** | `test_default_risk_free_rate.py` 4; `test_rate_dividend_service.py` 5 |
| `bs_greeks` closed form (◇) | ADR 0068 | `bs_greeks.py:49,111,141` | yes | degenerate inputs raise or return 0 by function | **implemented** | `test_bs_greeks.py` 38; cross-engine 361; BS-00x 7 |
| `(r, q)` facade | #2764 | `rate_dividend_service.py:40` | yes | dividend failure leaves q `None`; iv30 502, recorder stores q=0/"unknown" | **implemented** | `test_rate_dividend_service.py`; `test_iv30_router.py:230` |

### B. Cut candidates (reader's) and the driver's disposition

| path::symbol | what | proof | reader conf | driver disposition |
|---|---|---|---|---|
| `PythonDataService/_options_companion_report/` (7 files) | one-off generated report from specific April 2026 dates | `git grep "_options_companion_report"`: only its own generator | confirmed | **Out of scope** — the map's Out-of-scope list names "tracked output files in the `PythonDataService/` root … `_options_companion_report/` beside one-off generators" under Repo plumbing; pointer, not a row |
| `generate_options_companion_report.py` (460 lines), `validate_options_companion_timestep.py` (470 lines) | manual scripts | `git grep -w` returns only themselves | probable | same: repo plumbing, out of scope; pointer |
| `app/services/quantlib_pricer.py::implied_volatility` (`:300-400`) | second IV solver (QuantLib bisection) | no caller, no test (`git grep -n -E "quantlib_pricer.*implied_volatility"` hits only docstring `volatility/solver.py:7`); `options-math-authorities.md`: "No new IV solver … never a duplicate" | probable | **Not cut — finding:** `solver.py:7-8` provenance block names it as the companion with parity "pending-fixture"; cutting it means rewording a provenance claim and the owner's ◇ names "the IV solver"; owner ruling (#2704 addendum 2 cut `pricer.py` for "Validated against: NONE", so the precedent points to cut) |
| `Frontend/src/app/services/quantlib.service.ts::selectedEngine` (`:63-64`, read at `:91`) | signal never written | `git grep -n selectedEngine -- Frontend/src`: only the service; constant `'analytic_bs'` | probable | **Not cut — finding:** replacing the read with the literal is an edit, not a delete (F2 B5 same) |

### C. Not cut

`bs_greeks.bs_european_vega` (`:141`): ◇ kept by #2703 addendum rows 17-18; production callers none; tests `test_bs_greeks.py::TestBsEuropeanVega`, `tests/volatility/test_solver_parity_pyvollib.py:23,91`. `iv_recorder.set_iv_store`, `InMemoryIvSnapshotStore`: test-only seam (☆; `test_iv_recorder.py:223`, `test_edge_recorder_fallback.py:23-39`). `Iv30LiveRequest.debug`/`per_strike_contributions` (`iv30.py:58,75`): no UI sends `debug`; `test_debug_payload_when_requested` proves ◇ diagnostic. `QuantLib*Request.evaluation_date` (`quantlib_options.py:43,151`): ADR 0022 amend. 2026-10-09 "Left, with reasons". `/price` engine selector (`engine` arg Python `:45`, `QuantLibService` `:76`, GraphQL `quantlibPrice`) and `_numeric_*` fallbacks (`quantlib_pricer.py:264-297`): reachable only by direct GraphQL; removal is an API change across Python, C# and GraphQL snapshots — owner ruling. `/price` response fields `d1,d2,price,theta,vega,rho`; `OptionsExpirationsResponse.success/error` (never set non-default, `responses.py:41-46`): contract fields, wait on a serial regeneration. `iv30_parametric` (`iv_recorder.py:282`; `RecordedIvSnapshot`, `SnapshotResponse`): always `None`, ADR 0071 d3 "Owed". Stores and `/api/iv-recorder/snapshot`: opt-in, not dead by ruling. `bs-price-cross-engine` absent from `manifest.json`: documented exception (`golden/README.md:132-146`). `/api/quantlib/status`, `/api/quantlib/strategy`: gone (no Python route, Backend or Frontend reference). `lognormalCdf` (#2709 C14): `git grep -w` returns nothing — already cut.

### D. Finding rows

Lens 1:

| ID | path::symbol | sev | evidence | disposition | status |
|---|---|---|---|---|---|
| D-1 | `iv30.py:195,253`, `iv_recorder.py:251` → `polygon_client.py:751-757` | **P2** (fails closed; if recorder enabled, burn-in history is all error rows) | routers call `list_snapshot_options_chain(underlying_asset=…)` with no expiry; the client defaults `params["expiration_date"]` to today's ET date ("to avoid fetching thousands of contracts"); `_normalized_quotes_by_expiry` drops `days <= 0` (`iv30.py:140`) → 0 expiries → 502 `Need at least two expiries … got 0`. Under a fake SDK applying the vendor's exact-date filter both routes returned 502 and the vendor saw `{'expiration_date': '2026-10-14'}`. The default exists since 2026-02-17 (`36f68915`) and the route was added 2026-04-27 (`d6576bcd`), so it has never worked against the real client. Tests mock `polygon_client.list_snapshot_options_chain` itself. | fix: request a window of expiries or call without the default; add a router test through a fake SDK | traced + reproduced under a vendor-filter fake (needs one live curl). ADR 0071 d7 says 15:55 "has been recorded since 2026-04-28": owner should check whether `/var/lib/iv-recorder/*.jsonl` holds non-error rows |
| — | `iv30.py:4-6` (test docstring) and `iv30.py:190-193` | hygiene | claims live VIX acceptance runs "as a slow live test (skipped in CI)"; `git grep -i "published vix\|live.*vix"` finds none | fix: drop the claim or write the test | traced |

Lens 3:

| ID | path::symbol | sev | evidence | disposition | status |
|---|---|---|---|---|---|
| D-6 | `quantlib_options.py:353-369` | P2 | Greeks the engine does not provide are set to `0.0` ("Skip numeric … for speed"). `/compare` with 10 points: `quantlib_crr/jr/lr/fd` have `vega=0.0, rho=0.0` for every point; `quantlib_mc` `theta, vega, rho = 0.0`; Pricing Lab vega/rho chart plots flat zero lines | fix: omit the field or compute it | reproduced |
| D-7 | `quantlib_options.py:469-471,537-543` | P2 | `spot_min or …` treats `0` as unset; `spot_min=-50, spot_max=50` returns `success:true` with models `['python_bs','quantlib_mc']` (5 engines raised, skipped with a warning only); `spot_max<spot_min` gives a descending curve with 200 OK; Pricing Lab "Range %" `min=5 max=50` is an HTML attribute only | fix: validate `0 < spot_min < spot_max` (422); report skipped engines | reproduced |
| D-3b | `polygon_client.py:678` vs `:686` | P2 | a 4xx (`raise_for_status`) fails the request, but a timeout/connection error is retried then the window skipped (`:695`) → `success:true` with fewer expirations; partial indistinguishable from complete | fix: surface skipped windows | traced |
| D-5 | `market-data.service.ts:281-282`; `pricing-lab.component.ts:345-356` | P2 | `result.success ? result.expirations : []` drops `error`; empty dropdown + `console.error` | fix | traced |
| — | `edge-api.service.ts:149-152` | hygiene | both live-IV30 attempts swallowed to `null`; marker "non-load-bearing" which is why D-1 is invisible | leave-with-reason | traced |
| D-12 | `options_companion_service.py:169-175,195-197,380-382,583-588` | P2 | Polygon lookup failure becomes `None`/`[]`/`[]`; report records the day as "no listed expiry at trading_day + N days", conflating outage with no chain; only fail-loud check is "100% empty column" | fix | traced |
| — | `iv_recorder.py:302-311` | P2 | `log_iv_dominance_gate(max_share_before=…, max_share_after=…)` passes the same post-gate value twice, hard-codes `strikes_remaining=0`: burn-in diagnostic carries fabricated fields | fix | traced |
| — | `iv_recorder.py:280` | robustness | `_normalized_quotes_by_expiry` outside the `try`: unexpected quote shape returns 500 and writes no audit row against "row is still written" | fix | suspected |
| — | `iv30.py:136-139` | hygiene | `except (ValueError, TypeError): continue` silently drops non-anchor contracts | leave (client always builds anchors) | traced |

Lens 4:

| ID | path::symbol | sev | evidence | disposition | status |
|---|---|---|---|---|---|
| D-8 | `quantlib_options.py:191-227,480` | P2 (latent: Pricing Lab sends q=0) | `_scipy_bs_greeks` has no dividend parameter and re-derives d1/d2 and Greeks (duplicate of `black_scholes_greeks`, no parity test); with `dividend_yield=0.03` `python_bs` price 6.8865 vs `quantlib_bs` 6.4074; breaks "no new BS formula in any other Python file" (`options-math-authorities.md`) | fix: call `black_scholes_greeks(..., dividend=request.dividend_yield)` | reproduced |
| D-9 | `quantlib_options.py:266,452`; `quantlib_pricer.py:148,336` | P2 | `date.today()` is container-local (UTC) not ET; 20:00 ET–midnight an option expiring the next ET date gets `T=0` and "Option has expired"; DTZ011 ×4 | fix: `et_date_at_ms(now_ms_utc())` | traced |
| — | `quantlib_pricer.py:6-7`, `bs_greeks.py:5-6` | robustness | provenance says Greek-only cross-engine parity "pending-fixture"; ad hoc over the 360-case grid max abs diff for delta, gamma, theta, vega, rho is 5.0e-9 (8-dp rounding `quantlib_pricer.py:233-237`): equivalent in practice, unpinned | fix: Greeks parity test at `atol=1e-8` | reproduced |
| — | `options_companion_service.py:8-13` | hygiene | docstring says Greeks come from QuantLib `AnalyticEuropeanEngine`; code uses `black_scholes_greeks` (`:258`) | fix | traced |
| — | `options_companion_service.py:418` | hygiene | CSV `expiration` as `YYYY-MM-DD`; ADR 0022 (e)/(f) name no deviation; doc and tests pin it | owner-ruling | traced |
| — | `iv30.py:239,345`, `iv_recorder.py:247` | hygiene | `int(asof.timestamp() * 1000)` re-implements `to_ms_utc` | fix (low) | traced |
| — | ADR 0071 d7 | pointer | cron Mon-Fri, no calendar: holiday and half-day slots record a stale after-close snapshot as a normal row ("need no calendar") | leave-with-reason | traced |

Rate literals (question 5): resolved (#2764) in this slice. Only literal `risk_free_rate.py:26` `DEFAULT_RISK_FREE_RATE = 0.043`; importers `quantlib_options.py:36,148`, `models/requests.py:136`, `models/strategy.py:54`, `models/portfolio.py:93`, `fred_service.py:111,135,185`, `option_model_inputs.py:303`, `research/options/contract_finder.py:128-129`; no `0.05` rate default in this slice, Backend or Frontend (Backend only passes `decimal? riskFreeRate` through; Pricing Lab and Strategy Builder send `null`); prose-only 0.043 mentions `rate_dividend_service.py:7`, `dividend_service.py:8`, `fred_service.py:148`; `volatility/surface.py:186` docstring example `rate=0.05`.

Lens 5:

| ID | path::symbol | sev | evidence | disposition | status |
|---|---|---|---|---|---|
| D-2 | `options.py:47-51` + `polygon_client.py:658,678,737` | **P2 by the repo labels (no security tier); discloses a vendor credential** | `_fetch_window` puts `apiKey` in the query params and does not catch `HTTPError`; `err_msg` masks the key only in the log (`:737`), the original exception is re-raised and the router returns `detail=f"…{e!s}"`; patched `requests.get` → 403: 500 body `…for url: https://api.polygon.io/…&apiKey=<key>`; reachable by the browser through the `/api` dev proxy; loopback-bound | fix: generic `detail`, `raise … from` | **reproduced** |
| D-3 | `options.py` request; `polygon_client.py:700-727` | robustness | `gte > lte` → zero windows → `ThreadPoolExecutor(max_workers=0)` → 500 "max_workers must be greater than 0" instead of 422; 1000-year range built `max_workers=12175`; range to year 9999 → 500 "date value out of range"; no cap on window count | fix: validate order and cap range | reproduced (pool patched before any network) |
| D-4 | `polygon_client.py:658,727` | P2 | `limit: 1000`, no `next_url` follow, no sort param; if the vendor's default order is ticker-ascending, a high-strike underlying returns only the first ~1000 contracts per 30-day window (earliest expiries only) | fix: paginate or sort | **suspected** (needs a live probe; no test) |
| — | `iv_recorder.py:185-196` | robustness | `read_series` parses every line before filtering; each row carries full `raw_chain` (megabytes for SPY); history unbounded; every `/api/edge/*` call omitting `iv_series` scans the whole file | fix: Postgres cutover (ADR d8) or index | traced |
| D-10 | `iv_recorder.py:203` | robustness | `_DEFAULT_STORE_PATH = /var/lib/iv-recorder`, no env override, no compose volume (`git grep iv-recorder compose.yaml`: nothing): when enabled, history sits in the container layer and is lost on recreate | fix: volume or env path | suspected |
| — | `quantlib_options.py:434-566`, `:92`; `iv30.py:195-206`; `iv_recorder.py:81`; `options.py:32` | robustness (see driver note) | blocking sync I/O and CPU inside `async def` stalls the event loop; `/compare` at 100 points took 6.0 s (R5) / 140 s (F2 under load); `/price` with `engine=monte_carlo` (`quantlib_pricer.py:111`, `requiredTolerance=0.001`) had not returned after 75 s, no timeout; 7 sibling routers use `to_thread` | fix: `run_in_threadpool` and a tolerance cap | reproduced (MC exceeded 75 s) |
| — | `iv30.py:54`, `iv_recorder.py:38` | hygiene | `symbol` has no length/pattern; tickers not upper-cased so `spy.jsonl` ≠ `SPY.jsonl`; `confine_path_to_root` covers traversal | fix (low) | traced |
| — | `iv_recorder.py:39` | hygiene | service imports private symbols from a router (`app.routers.iv30`); `_pick_straddle_pair` raises `HTTPException` inside a service | fix | traced |
| — | OpenAPI vs Pydantic | none | `export_openapi_contract.py --check` exit 0; Pydantic, C# DTO (`QuantLibResponse.cs`), GraphQL snapshot and `types.ts` agree | nothing | reproduced |

Lens 6: no test of `POST /api/options/expirations` or `list_options_expirations` (only companion fakes `test_options_companion_service.py:284-343`) — add with D-2, D-3 as regressions; recorder error branches `replication_failed`, `insufficient_snapshot` untested; `/compare` QuantLib curves and non-analytic `/price` engines untested (`test_default_risk_free_rate.py:117` forces `_QL_AVAILABLE=False`) — would catch D-6, D-7, D-8; tests added since 2026-10-01: only `be0169d0` (`test_default_risk_free_rate.py`), clean and non-vacuous; `test_snapshot_route_records_a_slot` asserts the response, not the stored row (minor).

Lens 7: `docs/options-companion-format.md:47` lists `iso_time`, which the code does not emit (`_columns_for`, ADR 0022 (e)); `:173,:289` cite `_bsm_greeks` and `_MS_PER_YEAR`, which do not exist (`git grep -w _bsm_greeks` finds only the doc); `:175,:212` stale line numbers. `quantlib_options.py:441-446,495-498` says 801 steps / 801×800 / tol 0.001 (and 0.01 in one comment), actual 201 / 201×200 / 0.02 (`:293-307`); `pricing-lab.component.ts:93-117` repeats 801 and `tol=0.001`, the HTML `:436-446` is correct. `main.py:1414` names a non-existent route. `Query.cs:77` ("returns only date strings"), `iv_recorder.py:161-163` ("from request bodies (edge.py)") stale. ruff project scope on 10 slice files and 5 tests passed; `S110,E722,T201,F401,F841,ERA001` none. `known-gaps.md` names no slice file; D-1, D-2, D-4 would merit entries if not fixed.

Pointers outside the slice: `broker-options-chain.component.ts:~358` `q.iv ?? 0.2` feeds a placeholder IV into the engine-Greek column; `PythonDataService/{_spy_partial_parity_results.json,_validation_report.html,generate_validation_report.py,run_spy_partial_parity.py,study_*.txt}` tracked root-level one-off artifacts (named in the map's Out of scope); `vix_replication.py:6` still "Validated against: NONE"; other `requests.get(...apiKey)` call sites in `polygon_client.py` may leak the same way as D-2.

### E. Skipped/gated
None skipped/xfail/Postgres-gated in the 12 slice files. `test_bs_cross_engine_parity.py:71` has module-level `pytest.importorskip("QuantLib")`: collected 361 here (QuantLib 1.42.1), `requirements-light.txt:16` lists `QuantLib>=1.35`; had it been absent the whole module including the grid-size pin would skip silently. No parity test collected zero cases. `test_iv30_router.py` is mocked at the client import boundary; the "slow live test" it names does not exist.

### F. Not read
`Backend/GraphQL/Query.cs` and `PolygonService.cs` (only QuantLib, expirations, recorder spans); `IvRecorderRegistration.cs` (guard branches skimmed); `app/volatility/*` (call signatures only); `edge.py` (only `_iv_series_from_recorder`); `polygon_client.py` (only `list_options_expirations` and `list_snapshot_options_chain`); BS-00x fixtures confirmed running not audited; kill-list dumps grepped for slice terms only. Live Polygon never contacted: D-1 and D-4 need one live call each.

## F1 — Frontend Strategy Builder + browser Black-Scholes copy (read at f1288f2d)

Headline: no order path; no P0. Numbers the page shows diverge from the server (D1–D3, P2; D1 is the candidate for owner rating P1). Parity covers the BS price kernel only (360 cases, `atol=1e-4`). H4 (16:00 local expiry) and the second default rate (0.05) are resolved. `lognormalCdf` is gone (#2754).

### What the browser computes

| quantity | browser source | Python canonical | parity test |
|---|---|---|---|
| BS price | `bsPrice` | `bs_greeks.py::bs_european_price` | **yes** (price only, `black-scholes.parity.spec.ts`, 360 cases from `src/testing/bs-parity/grid.json`, asserts `cases.length===360`) |
| per-leg Greeks | `bsDelta/Gamma/Theta/Vega/Rho` | `bs_greeks.py::black_scholes_greeks` | none |
| strategy P&L and Greek curves | `strategyPnlAtPrice`, `strategyGreekAtPrice` | `strategy_engine.py::compute_current_curve`, `compute_greek_curves` | none |
| live Greeks mini-row | `liveGreeks` (ts:838) | `compute_strategy_greeks` | none |
| expiry payoff | `livePayoffCurve` (ts:676) | `compute_payoff_curve` | none |
| breakevens | `liveBreakevens` (ts:695) | `find_breakevens` | none |
| max profit / loss, net cost | `liveMaxProfit`, `liveMaxLoss`, `netCost` | `compute_max_profit_loss`, `compute_strategy_cost` | none |
| what-if curves | `whatIfCurves` (ts:724) | none | none |
| rho | Greek select | none (server has no rho) | none |

### A. Feature table (route `options-lab/strategy-builder`, `app.routes.ts:103-109`; reached from the Options Lab tab and the broker-options-chain page-guide link)

| feature | requirement | entry | normal / empty / failure | class | evidence |
|---|---|---|---|---|---|
| Load ticker → expirations, snapshot, nearest-expiry chain | ADR 0022 amend. 2026-10-09 (#2934); picker ADR 0066 | `onTickerPicked`/Load → `fetchExpirations` (ts:143, 940) | normal OK; empty/`success:false` = blank page, no message (D9); `errors` banner untested | partial | spec SB-A ×2 |
| Select expiry, price at that expiry's tenor rate | #2764, #2789 | `onExpirationSelected` (ts:980) | normal OK, late replies ignored; after failure old chain stays (D11) | implemented (normal) | spec "risk-free rate" ×5 |
| Chain table ±N strikes / All / ATM, volume bars, Quick/Full Greeks (persisted) | UX-Q2, UX-Q4 | `visibleRows` (ts:500) | density OK; window/ATM untested | density implemented; rest unverified | spec UX-Q2 ×2 |
| Add legs from chain (≤8), remove, qty stepper, clear | UX-Q4 | `addLegFromChain`, `removeLeg`, `setLegQty`, `clearLegs` | at 8 legs silent no-op (D13) | unverified | only `clearLegs` exercised |
| 16 strategy templates | UX-Q4 | `applyTemplate` (ts:1101) | needs spot + strikes else silent no-op | unverified | none |
| Expiry payoff, BE, Max P/L, net cost (browser) | ADR 0068 exception 1 | `livePayoffCurve` etc. | renders; numbers diverge (D1, D2) | partial | none (only `noForwardCurves`) |
| Current-time P&L curve, Greek curve, what-ifs, live Greeks (browser BS) | ADR 0068 exception 1 | `currentPnlCurve`… | gated on rate and T>0; wrong for iv=0 legs (D3) | partial | rate-gate test; price kernel parity-pinned |
| Server analysis, auto-analyze (300 ms debounce): POP, EV, Max P/L, BEs | #86, #2764 | `analyzeStrategy` (ts:1150) | normal/resolver error/network error covered; EV unit defect (D4) | implemented | SB-C ×4, auto-analyze ×2 |
| Contract history drawer | UX-Q1/R0b | `openContractHistory` (ts:199) | normal + null contract tested; error/empty untested; stale "2-year" copy (D18) | implemented (normal) | UX-Q1 ×4 |
| Zero-TTM banner | UX | `noForwardCurves` (ts:672) | OK; positive case time-of-day dependent (D20) | implemented | 4 tests |
| Save/load a strategy | none | none | none | missing (no requirement) | none |

### B. Cut candidates (lens 2)

| # | `path::symbol (lines)` | what | proof it is dead | what else goes | conf |
|---|---|---|---|---|---|
| B1 | `shared/payoff-chart/payoff-chart.component.ts:30-32` (`breakevens`, `spotPrice`, `timeToExpiry` inputs) + comment line 24 | declared inputs never read | no `breakevens()`/`spotPrice()`/`timeToExpiry()` in .ts/.html; one caller `strategy-builder.component.html:486`; leftover of #2747 item 6 (#2754 cut only `weightedIv`, `riskFreeRate`) | bindings `strategy-builder.component.html:492-494` | confirmed |
| B2 | `strategy-builder.component.ts` `BuilderChainRow.callPriceNum`, `callIv`, `putPriceNum`, `putIv` (49-50, 59-60; 569-570, 579-580) | row fields computed, never read | `git grep -w` returns only those lines (`callIv`/`putIv` also match locals in options-lab-chain — different symbol) | — | confirmed |
| B3 | `utils/occ-ticker.ts::OccDisplayParts.expDateShort` (38, 111) | display field nothing reads | `git grep -w expDateShort`: only occ-ticker.ts + spec | `occ-ticker.spec.ts:62,72` lose that key | confirmed |
| B4 | `strategy-builder.component.scss:119-184` (`.preset-grid`, `.preset-group-divider`, `.preset-card`, `&.preset-custom`), `:419` `.template-select,`, `:432-435` `optgroup{}` | CSS of pre-UX-Q4 preset-card grid/select | class scan: 7 of 100 classes unused; `git grep -w` hits only scss | — | confirmed |
| B5 | `ts:319-352, 478-481` (`presetGroups` labels, `customPreset`) | group labels never rendered | `git grep -w presetGroups`: ts only, sole consumer `flatTemplates` | an edit, not pure delete | probable |
| B6 | `ts:90-97, 199-213` (`SelectedHistoryContract.contractType/strikePrice/expirationMs/snapshot`, `side` param) | written, never read; only `.ticker` read | only the spec reads them | call sites html:228,303; spec 448, 484-488, 505-509 | confirmed (edit) |
| B7 | `ts:83` `LegConfig.enabled` + 10 filters | no UI toggles it; always `true` | no setter to false, no template control | spec 261-282, 743-750 | probable (edit) |
| B8 | Frontend half of opt-in analysis flags: `graphql/types.ts:136-142,156-181,199-201`, `market-data.service.ts` `options` param + variables + three selections | nothing sets `includeCurrentCurve/GreekCurves/LegDiagnostics` or what-if shifts; always `false`/`null` | `git grep -w includeCurrentCurve` outside Backend + service: type file only; the caller passes 5 args | `market-data.service.spec.ts`, strategy-builder spec fixtures `currentCurve:null` | probable |

### C. Not cut

- `utils/black-scholes.ts` all exports (live callers in strategy-builder and pricing-lab; named exception, ADR 0068; `engine-authority-map.md:153`).
- `black-scholes.parity.spec.ts`, `grid.json`, `scripts/generate-bs-parity-fixture.py` — Sacred. `black-scholes.spec.ts` proves in-module helpers.
- `bsRho` (Pricing Lab + Greek select). `PayoffChartComponent` whole (one live caller). `date-validation.ts::getMinAllowedDate`. `occ-ticker.ts::parseOcc`.
- Python `compute_current_curve`, `compute_greek_curves`, `compute_leg_diagnostics` and Backend resolver args (`Backend/GraphQL/Query.cs:296-300`): test-proven options math (◇), proved by `tests/test_strategy_engine_phase1_1.py`; after B8 nothing outside tests calls the flags (pointer).
- Accessibility-class: roles/aria in the html, `.density-toggle aria-pressed`, `ttm-zero-banner role="status"`.
- Pointers: `strategy_engine.py` header, `app/models/strategy.py`, `tests/test_strategy_engine_phase1_1.py` cite nonexistent `OptionsStrategyLabComponent` and commit `451394d`; `docs/references/iv-rv-basis-alignment.md:91` lists "strategy-builder hardcoded r=0.043" (fixed by #2764); `docs/known-gaps.md:90-95` names Strategy Builder among `new Date(string)` sites — this file has none.

### D. Finding rows

| # | where | lens | sev | evidence | disposition | status |
|---|---|---|---|---|---|---|
| D1 | `ts:641-662, 793-806` | 4/1 | P2 (candidate P1) | browser Max Profit/Loss = min/max over ±5% window around strike midpoint; tail flags cover calls only (ts:775-785). Short put 590@5 (`naked_put` template) shows Max Loss −$2,450; server card −$17,200 (±30% window, `strategy_engine.py:100`); true −$58,500. Long put Max Profit clipped the same way | fix: analytic S=0 for puts or show server number | reproduced (node replay + Python canonical) |
| D2 | `ts:695-709, html:399` | 4/1 | P2 | breakevens outside the ±5% window vanish: long strangle 570/610@10 → browser `[]` (BE row hidden, html:399), server `[550,630]`; client counts zero touches (`<=`), server strict `<0`; 2 dp vs 4 dp (H3) | fix: same window as server | reproduced |
| D3 | `black-scholes.ts:142` vs `strategy_engine.py:404` | 4 | P2 | `bsPrice` returns 0 when `sigma<=0` and t>0; Python values at intrinsic. `addLegFromChain` sets `iv = impliedVolatility ?? 0` (ts:1043). Long 95 call @6, S=100, 30d: TS −6 at every spot; Python −6/−1/+4 at 95/100/105 | fix: intrinsic when iv≤0 or hide curve; add to owed parity | reproduced |
| D4 | `html:521` vs 525/529, `ts:828,835` | 1 | P2 | EV printed per share; Max Profit/Loss same card ×100 per contract → EV reads 100× too small | fix | traced |
| D5 | `ts:737` vs `strategy_engine.py:~450` | 4 | robustness | what-if IV floor 0.01 browser vs 0.0 Python; rho browser-only | leave (owed parity) | traced |
| D6 | `generate-bs-parity-fixture.py:60-88`; ADR 0068:28,31 (H2) | 4/7 | hygiene | generator carries own inline BS copy; docstring claims bit-for-bit; differs at t≤0 or vol≤0 (intrinsic vs 0.0, `bs_greeks.py:125`); no Python test reads `grid.json`; not in golden manifest; measured agreement today 1.4e-14 | fix (#2747 follow-up 5: import `bs_greeks.py`) | traced + measured |
| D7 | `types.ts:109`; ts | 4 | hygiene | snapshot delivers `dividendYield`, page ignores it (q=0 both browser and server) | owner-ruling | traced |
| D8 | `ts:608-611` | 3 | P3 | `daysToExpiry` computed over `Date.now()` recomputes only on expiry change → stale time on a page left open; server uses fresh time | fix | traced |
| D9 | `market-data.service.ts:~257`, `html:94,321` | 3 | P2 | `getOptionsExpirations` maps `success:false` to `[]`, drops `error`; ribbon and "No contracts" need length>0 → vendor failure and no-options ticker both blank | fix | traced |
| D10 | `ts:957-958` | 3 | robustness | `getStockSnapshot(...).catch(() => null)` silent | leave or log | traced |
| D11 | `ts:980-1012` | 3 | P2 | `onExpirationSelected` clears legs, not `allContracts`; on failure old expiry's chain stays under new ribbon selection and legs can be added | fix | traced |
| D12 | `ts:940-977` | 3/5 | P2 | `fetchExpirations` has no request token (`fetchChainSnapshot` does) → ticker A then B can leave A's expirations under B | fix | traced |
| D13 | `ts:1040` | 3 | P3 | 8-leg cap returns before "existing leg, qty +1" (ts:1045), silently | fix | traced |
| D14 | `ts:968-969` | 4 | P2 (self-correcting) | `expirations.find(e => e >= todayAnchor)` uses today's 16:00 ET not now → after close selects expired expiry | fix | traced |
| D15 | `ts:1185` | 3 | P3 | in-flight analysis for older legs writes `analysisResult` after user edited, until follow-up lands | leave | traced |
| D16 | `ts:172-176,181-183` | 3 | robustness | `localStorage` read/write unguarded | fix | traced |
| D17 | `market-data.service.ts:161-170`, `types.ts:186-202` | 5 | hygiene | query requests `symbol, spotPrice, strategyCost, curve, greeks` + opt-in fields; page reads only success/pop/EV/maxProfit/maxLoss/breakevens/error; server builds 300-point curve + Greeks each debounce | fix (trim) | traced |
| D18 | `ts:220-221`, `date-validation.ts:1-7`, `html:590-592,621,625` | 4/7 | hygiene | UTC date string + re-derived 2-year floor; `PythonDataService/CLAUDE.md`: history is 5 years and floor is `polygon_history_floor()`; copy says "Polygon Starter (2-year window)" | fix | traced |
| D19 | `spec:504-516` | 6 | test bar kind 3 | repeats `occ-ticker.spec.ts:58-66`; survivor `occ-ticker.spec.ts:58` | delete | traced |
| D20 | `spec:712-725` | 6 | test bar | `expectedBanner = timeToExpiry() <= 0` mirrors implementation; true only after 16:00 ET | fix | traced |
| D21 | `date-validation.spec.ts:13-19` | 6 | kind 1 | re-implements function body; `:21` trivial (pre-10-01) | leave | traced |
| D22 | `black-scholes.spec.ts:326-331,364-367,381-389` | 6 | hygiene | weak assertions (pre-10-01) | leave | traced |
| D23 | specs | 6 | gap | no spec for `addLegFromChain`, `applyTemplate`, `visibleRows`, `livePayoffCurve`, BE, Max P/L, `whatIfCurves`, `payoff-chart` | owner-ruling | — |
| D24 | `black-scholes.ts:1-46` | 7 | hygiene | header cites nonexistent `OptionsStrategyLabComponent`, commit `451394d`, "Phase 1.2/1.4" | fix: trim to ADR 0068 | — |
| D25 | `black-scholes.ts:57` vs `types.ts:226`, `ts:756` | 7 | hygiene | `GreekName`/`GreekType` same union; needless `as GreekName` | fix | — |
| D26 | `ts:308` | 7 | hygiene | `priceRangePct` never-set signal | leave | — |
| D27 | `ts:1226` | 7 | hygiene | `toLocaleString()` without locale | leave | — |

Measured: `grid.json` vs `bs_european_price` max abs diff 1.4e-14 (360); TS `bs*` vs Python `bs_greeks` 1,260 cases: price 1.6e-5, delta 7e-8, theta 1e-9, rho 1.4e-7, gamma/vega <5e-16; `strategyPnlAtPrice`/`strategyGreekAtPrice` equal Python at 4/6 dp for a 95/105 bull call spread; diverge for an iv=0 leg (D3).

### E. Skipped/gated: none in slice. Parity spec 2 tests / 360 cases; scheduled `Frontend/ci-spec-durations.json:455-456`; `grid.json` not in golden manifest (D6).

### F. Runs and not read
ng test 3 files **82 passed** (28 + 52 + 2), 0 skipped, vitest root = worktree; eslint `--max-warnings 0` on slice: clean; `tsc --noEmit --noUnusedLocals --noUnusedParameters`: 0 errors in the slice (six TS6133 elsewhere: `bot-panel-shell.component.ts:304,443`, `data-lab-chart.component.ts:342`, `edge-api.service.ts:219`, `spec-strategy-runner.component.ts:523`, `fake-symbol-catalog.ts:70`). Not read: scss beyond a class-use scan, expiration-ribbon/symbol-picker/chart components, `tests/test_strategy_engine*.py`; no browser run (D1–D3 are code-level reproductions).

## F2 — Frontend Options Lab shell + chain, expiration ribbon, Pricing Lab, QuantLib service (read at f1288f2d)

Runs: `ng test` on `pricing-lab.component.spec.ts` (11), `options-lab-chain.component.spec.ts` (8), `options-lab-chain.component.render.spec.ts` (1), `quantlib.service.spec.ts` (1): **21 passed**, 0 failed, 0 skipped, vitest root = worktree; eslint `--max-warnings 0` on options-lab, options-chain-v2, pricing-lab, `quantlib.service{,.spec}.ts`, `app.routes.ts`: 0 problems; `tsc --noEmit --noUnusedLocals --noUnusedParameters`: 6 TS6133 outside the slice (`bot-panel-shell.component.ts:304,443`, `data-lab-chart.component.ts:342`, `edge-api.service.ts:219`, `spec-strategy-runner.component.ts:523`, `shared/symbol-catalog/testing/fake-symbol-catalog.ts:70`); `ruff check app/routers/quantlib_options.py` clean, isolated select DTZ011 ×2 (`:266,:452`), BLE001 ×1 (`:537`); `tests/services/test_default_risk_free_rate.py` 4 passed. Scratch scripts `compare_block.py`, `engine_time.py` in the scratchpad.

### A. Feature table

| feature | requirement | entry | reachable | normal / empty / failure | class | evidence |
|---|---|---|---|---|---|---|
| Options Lab shell, tab nav | flow (45fe5bd6) | `/options-lab` → `chain` (`app.routes.ts:88-97`) | yes: menu `shell/app-menu.ts:57` | badge stays `SPY` for any ticker (`options-lab.component.html:5`) | implemented, badge false | no spec for shell or route table |
| Chain grid (±N strikes/All, density, ATM/ITM, volume bars) | flow; ADR 0022 amend. (#2934) | `/options-lab/chain` | yes | empty "No options data available for this expiration." (`chain.html:117`); failure banner only for non-silent fetches | implemented | `options-lab-chain.component.spec.ts` (6 behaviour tests) |
| Chain header spot price / change | flow | same | yes | price ok (`stockSnapshot.day.close`); change and % wrong (D2) | partial | traced; only test data has distinct change/% (`spec:60`) |
| Expirations load, nearest pick, ribbon | flow; #2934 | `fetchExpirations` (`chain.ts:258`) | yes | `success:false` → `[]` shown as empty state (D5) | unverified | no test drives `fetchExpirations`; no ribbon spec |
| 5 s silent poll | flow; Codex P2 #2 comment | `ngOnInit` (`chain.ts:236-255`) | yes | silent failure swallowed, stale grid stays, no as-of time (D6) | unverified | the "polling" test is vacuous (D25) |
| L/S "add leg" buttons | none (comment `chain.ts:348` "stub: future integration") | every row | yes (visible) | click does nothing | missing | `git grep -w addLeg` in options-lab: only this component |
| Strategy finder tab | none; "BETA" badge (`options-lab.component.html:27`) | `/options-lab/strategy-finder` | yes (tab) | static "COMING SOON" | missing (placeholder) | no spec |
| Volatility tab | owner ruling: "stub Volatility page go" (map #2700 ◇; #2706 cascade) | `/options-lab/volatility` | yes (tab) | static placeholder | missing (placeholder, ruled dead) | no spec |
| Pricing Lab load (expirations, chain, FRED rate prefill) | #2764, #2934; ADR 0068 | `/pricing-lab` | yes: `app-menu.ts:60`, links `broker-options-chain.component.html:14`, `broker-options-surface.component.html:16` | failure swallowed to `console.error`, `snap.success` ignored (D8) | partial | `pricing-lab.component.spec.ts` PL-A, 3 tests |
| Pricing Lab compare (python_bs + 6 QuantLib + TS overlay, diff chart/stats) | ADR 0068:31 (non-conformance), #2747 F6/H5 | `onContractSelect` / Compare | yes | normal ok; 0DTE "Option has expired" (D4); partial engine loss hidden (D9); event-loop stall (D21) | implemented, defects | PL-B 5, PL-E 3; QuantLib engines never run in any test |
| `QuantLibService.priceOption` (engine Greek column) | #2764 | `/broker/options-chain` (`app-menu.ts:58`, caller `broker-options-chain.component.ts:380`) | yes | failure leaves column blank by design (`:394-397`) | unverified | `quantlib.service.spec.ts` 1 test (request shape); caller has no spec |
| Expiration ribbon | flow; #2934 | `options-lab-chain.component.ts:19`, `strategy-builder.component.ts:24` | yes | n/a | implemented | no spec |

### B. Cut candidates

| # | symbol (lines) | what | proof | goes with it | conf |
|---|---|---|---|---|---|
| B1 | `options-lab/volatility-stub/volatility-stub.component.ts` (whole); `app.routes.ts:118-124`; tab anchor `options-lab.component.html:29-34` | Volatility placeholder page | `git grep -n -w -E "VolatilityStubComponent\|volatility-stub"`: only `app.routes.ts:121-122` and its own file. Owner-ruled: map #2700 ◇ "…and the stub Volatility page go"; #2706 dead-routes cascade names `VolatilityStubComponent` under "#2708 and #2709 (Frontend)" (`docs/research/lean-and-mean/dead-routes.md:220` on `research/lean-dead-routes`); #2754/#2756 never touched it (`git log --follow`: creation commits only); linked from shell tab bar so a missed row, not an unlinked-route case | nothing (no spec, doc, e2e) | confirmed |
| B2 | `options-lab-chain.component.ts:32-33` (`ChainRow.distance`, `distancePct`), `:186-187` (locals), `:198-199`, `:392-395` (`formatSigned`) | row fields computed, never rendered | `git grep -n -E "\.distance\b\|\.distancePct"` under options-lab: 0; template has none; specs do not read; `git grep -w formatSigned`: only these two uses; #2709 C28 removed `atmRowIndex` but left these | nothing | confirmed |
| B3 | `chain.ts:347-350` `addLeg`; `chain.html:129,172` (`ls-col` headers), `:185-191`, `:249-255` (buttons); `chain.scss:425-492` (`.ls-col/.ls-cell/.ls-btn`) | no-op L/S "add leg" column | body is `void strike; void right; void action;`; only callers are its own buttons | header colspans `chain.html:123,125` 10/5/4 → 9/4/3; aria-labels go with the buttons | probable (build-or-drop owner call; nothing in repo promises it) |
| B4 | `pricing-lab.component.scss:228-232` `.tt-diff` | CSS rule | `git grep -n tt-diff`: only the scss; tooltip HTML (`pricing-lab.component.ts:672-683`) emits `tt-title`, `tt-row`, `tt-dot` | nothing | confirmed |
| B5 | `quantlib.service.ts:64` `selectedEngine`, `:76` `engine?`, `:91`, import `:7`; `graphql/types.ts:209` `QuantLibEngine` | engine-toggle leftover | `git grep -w selectedEngine`: only `:64`/`:91`, never `.set`; `priceOption` one non-test caller passes spot/strike/volatility/expirationMs/optionType only; `QuantLibEngine` used only here | GraphQL `$engine` variables can stay (server default); spec's single test holds | probable |

### C. Not cut

- `strategy-finder-stub` (component, route `app.routes.ts:111-116`, tab `options-lab.component.html:21-28`): linked from shell, so not "routed-but-unlinked"; no owner ruling names it (unlike Volatility stub) — owner ruling: build or drop.
- `ExpirationRibbonComponent` (`options-chain-v2/expiration-ribbon/`, 3 files): live (`options-lab-chain.component.html:85`, `strategy-builder.component.html:96`); only the folder name is stale.
- Unused optional parameters `priceOption.riskFreeRate/dividendYield/evaluationDate`; `comparePricingModels.dividendYield/evaluationDate` (`market-data.service.ts:358-381`); `getOptionsExpirations.options.{contractType,expirationMsGte,expirationMsLte}` (`:255-260`): server-contract pass-throughs (spec proves "no rate sent"); ADR 0022:104 keeps `evaluation_date`, Python tests use it.
- `black-scholes.ts` use in Pricing Lab (`pricing-lab.component.ts:249-278`): ADR 0068:31 records Pricing Lab as non-conformance (F1's file; D13).
- Verified gone (`git grep -w` 0 hits, Backend and Frontend): `lognormalCdf`, `weightedIv`, `atmRowIndex`, `priceStrategy`/`QUANTLIB_STRATEGY_QUERY` (C7), Backend `quantlibStrategy`/`QuantLibStatus`. `_scipy_bs_greeks` (`quantlib_options.py:191`) live (called `:480`) — D15.
- Accessibility-class: `role="tablist"` on `<a role="tab">`, `role="toolbar"`, `aria-pressed`, `aria-live`, icon-only `status-dismiss` button, `:focus-visible` rules.

### D. Finding rows

| # | where | lens | sev | evidence | disposition | status |
|---|---|---|---|---|---|---|
| D1 | `options-lab.component.html:5` | 1 | P2 | static `<span class="ol-badge">SPY</span>` while chain has a symbol picker (`chain.html:30`) | fix: bind or delete; `.ol-badge` css | traced |
| D2 | `PythonDataService/app/services/polygon_client.py:779-780` → chain header `spotChange` (`chain.ts:111-112`, `html:23-26`) | 1/3 | P2 (P1 arguable) | `change` and `change_percent` both read `underlying_asset.change_to_break_even`; SDK `UnderlyingAsset` (`polygon/rest/models/snapshot.py:192-199`) has no change fields — this is the first contract's break-even delta, shown as spot move with pos/neg colour; chain page already fetches correct `stockSnapshot.todaysChange*` (`chain.ts:273`); no test | fix | traced (SDK read) |
| D3 | `chain.ts:119` `daysToExpiry` | 1 | hygiene | `Math.ceil((exp - Date.now())/86_400_000)`: 16:00 ET anchor reads "1 DTE" all session on expiry day, +1 other days; not recomputed | leave or owner call | traced |
| D4 | `pricing-lab.component.ts:348-349,447-453` + `quantlib_options.py:457-467` | 1 | P2 | default pick is today's expiry (`e >= todayAnchor`); `/compare` returns `success:false "Option has expired"` when whole-day T ≤ 0; lab shows "…The computation may have timed out — try again." (`:450`) wrong for that cause | fix | traced |
| D5 | `market-data.service.ts:281-283` | 3 | P2 | `return result.success ? result.expirations : []` drops vendor failure and error text; Options Lab "No options data available", Pricing Lab nothing | fix | traced |
| D6 | `chain.ts:244-254,313-327` | 3 | P2 | silent poll sets no error and no staleness cue; old quotes with no as-of time; poll guard (`:251`) cannot stop overlapping silent polls (silent fetches never set `chainLoading`) | fix | traced |
| D7 | `chain.ts:273` | 3 | hygiene | `firstValueFrom(getStockSnapshot).catch(() => null)` silently falls back to `underlying.price` | leave or log | traced |
| D8 | `pricing-lab.component.ts:354-356,383-385,367-382` | 3 | P2 | errors to `console.error` only, no `statusMessage`; `fetchChain` never checks `snap.success` → empty selectors, no message | fix | traced |
| D9 | `quantlib_options.py:508-553` + `pricing-lab.component.ts:456-461` | 3 | P2 | failing engine `logger.warning` + skipped (BLE001 `:537`); QuantLib missing → `success:true` with 1 model; lab says "Loaded N server model(s)"; no per-engine failure in response; docs promise 8 models | fix | traced |
| D10 | `pricing-lab.component.html:187` | 3 | hygiene | `(c.impliedVolatility ?? 0) * 100` shows unavailable IV as "0.0%" | fix | traced |
| D11 | `chain.ts:313-322`, `pricing-lab.component.ts:361-388,395-398` | 3 | P2 | response not tied to request ticker/expiration; silent poll for ticker A can land after ticker B and overwrite `allContracts`/`underlying`; self-corrects next poll; Pricing Lab same last-to-arrive-wins race | fix | traced |
| D12 | `broker-options-chain.component.ts:394-397` | 3 | hygiene | documented empty `catch {}`; QuantLib outage blanks column, no log | leave | traced |
| D13 | `pricing-lab.component.ts:205,249-278` | 4 | owed | default `diffReference='legacy_bs'`; TS BS price + 5 Greeks overlay is a math copy outside Python; ADR 0068:31 non-conformance (#2747 F6/H5); no filed issue for follow-up 4 (`gh issue list --search`); `black-scholes.ts:21-27` header still "LEGACY-OK (intentional)" | owner-ruling / pointer | traced |
| D14 | `quantlib_options.py:266,452` | 4 | P2 | `date.today()` on a UTC container: after 20:00 ET (EDT) T one day short and "tomorrow ET" expiries read as expired; ruff DTZ011 ×2; whole-day T ADR-sanctioned, server-local clock is not | fix (ET date from calendar module) | traced |
| D15 | `quantlib_options.py:191-227` `_scipy_bs_greeks` | 4/7 | hygiene | inline second copy of closed-form Greeks vs canonical `bs_greeks.py:49` `black_scholes_greeks`; only router calls it; no test names it (`test_bs_cross_engine_parity.py` covers `bs_greeks.py`) | fix or owner (the "scipy" column deliberately independent) | traced |
| D16 | `expiration-ribbon.component.ts:27` | 4 | hygiene | `new Date().getFullYear()` (viewer zone) compared with ET-derived `group.year` | fix (`etIsoDate(Date.now())`) | traced |
| D17 | `pricing-lab.component.ts:379-381` | 1/4 | hygiene | snapshot `dividendYield` fetched and ignored; only r applied → curves price at q=0 (`comparePricingModels` `:434-444`); `responses.py:121-125` docstring says pages use it; TS overlay has no q | owner-ruling | traced |
| D18 | chain `centerIv`, `resolveLast` mid (`chain.ts:188-190,369-372`) | 4 | pointer | #2747 F12 low-priority follow-up, already decided | pointer | n/a |
| — | rates | 4 | none | #2764: no `0.043`/`0.05` literal in Frontend slice; `pricing-lab.component.html:359` still says "(default 5%)" | fix copy | traced |
| — | `new Date(string)` | 4 | none | 0 in options-lab/ribbon/pricing-lab/quantlib; `docs/known-gaps.md:91-96` names "option-expiry, Options Lab" — stale | edit entry | traced |
| D19 | TS vs Pydantic vs OpenAPI snapshot | 5 | none | compared field by field against `contracts/openapi/python-data-service.openapi.json` and `contracts/graphql/backend.schema.graphql:290-291`: `QuantLibPriceResult` (11) = `QuantLibGreeksResponse`; `PricingCompareResult` (8) = `PricingCompareResponse` (`riskFreeRate: number\|null` vs required float: null only when Backend call fails); Backend DTOs equal | none | traced |
| D20 | `quantlib_options.py:35,147` | 5 | P2 | `risk_free_rate` unbounded while siblings use `ge=0, le=0.5` (`models/strategy.py:54`, `portfolio.py:93`); `spot_min/spot_max` unvalidated, `request.spot_min or ...` treats 0 as unset; client: clearing "Range %" → `rangePct=0`, zero-width range, duplicate chart times (`pricing-lab.component.ts:258-263,432`); `step="0.01"` on a decimal rate invites "5" | fix | traced |
| D21 | `quantlib_options.py:435` `async def pricing_compare` | 5 | **P1 suspected impact; stall reproduced** | one `/compare` (SPY-like, 100 points) took **140.4 s** with the event loop stalled 140.4 s: QuantLib MC at `requiredTolerance=0.02` ≈1.3 s/spot (analytic/binomial/FD ≈0); no `to_thread`; repo's own pattern `alpaca_clerk_sqlite.py:7`, `broker_configuration.py:15` ("stalls the event loop for every other in-flight request"); `loop_lag.py:1-12` names lease expiry and `FEED_DEATH` as the symptoms; same process hosts fleet lane heartbeat (`main.py:272-291`); auto-runs on every contract pick (`onContractSelect`); Backend 300 s timeout (`Program.cs:53`) cannot cancel it; timing under 7-reader load, page's own estimate 30-60 s; downstream lease/feed effect not reproduced | fix (thread-offload, cap MC) | reproduced (stall), suspected (impact) |
| D22 | model cards `pricing-lab.component.ts:93,100,107,114,121-122` | 5/7 | hygiene | card text says 801 steps / 801×800 / tol 0.001 (that is `/price`, `quantlib_pricer.py:98-112`); `/compare` uses 201 / 201×200 / 0.02 (`quantlib_options.py:293-306`); docs section says 201 (`html:435-445`); `/compare` docstring still says 801 (`:441-445`), a comment 0.01 (`:498`); lab says "30-60 seconds" (`html:273`) | fix copy | traced |
| D23 | `app/routers/snapshot.py:46-103` | 5 | suspected | `async def` chain snapshot does sync Polygon pagination + FRED/dividend calls on the loop, polled every 5 s per open tab (`POLL_INTERVAL_MS`); not measured | pointer R5 | suspected |
| D24 | `graphql/types.ts:112` | 7 | hygiene | comment "FRED-sourced, ~30d tenor"; Python now interpolates at expiry tenor (`responses.py:121-125`) | fix | traced |
| D25 | `options-lab-chain.component.spec.ts:173` "skips polling when no expiration is selected" | 6 | kind 1/4 | asserts initial `null`, `drainAll` flushes, `match(...)` always 0; 5 s interval never elapses in a sync test; cannot fail; added 2026-05-05 (45fe5bd6), survived #2734 | delete | traced |
| D26 | coverage gaps | 6 | note | no spec for shell, ribbon, `fetchExpirations`/poll render path, `broker-options-chain` (QuantLibService's caller), Pricing Lab failure paths, `syncChartData` (lightweight-charts mocked), `/compare` with real engines (`test_default_risk_free_rate.py:112-120` monkeypatches `_QL_AVAILABLE=False`) | note | — |
| D27 | `pricing-lab.component.ts:376-378` | 7 | hygiene | comment cites "Step 8 of IV-RV alignment" (plan note) | fix | — |
| D28 | `strategy-finder-stub.component.ts:55-62`, `volatility-stub.component.ts` | 7 | hygiene | "Tracked in GitHub issues (label: ...)" and "VCR-P3-N": no such labels/issues | goes with B1; owner call for strategy-finder | — |
| D29 | `pricing-lab.component.html:359,420,523` | 7 | hygiene | "default 5%" stale; "same implementation used in the live option chain pricing" false (only importers of `black-scholes.ts` are pricing-lab and strategy-builder); "Timeout: 300 seconds" | fix copy | — |
| D30 | `responses.py:125` → `api/broker.types.ts:24058` | 7 | pointer | docstring names retired page "options-strategy-lab" | pointer R5 | — |

Surviving tests vs the bar: 21 read; all assert behaviour or request contract; `pricing-lab.component.spec.ts:264` (overlay uses server rate) discriminating; `:138` (rate exactly 0) state-branch.

### E. Skipped/gated: none (no `skip`/`xit`/`xfail`/`todo`/`only`, no Postgres/network gating); no parity test in slice (`quantlib.service.spec.ts` is a request-shape test); `tests/services/test_default_risk_free_rate.py` 4 passed; its compare test disables QuantLib on purpose.

### F. Not read
`chain.scss` (12.5 KB) and `pricing-lab.component.scss` (8 KB) checked by class-reference script only (66 + 46 classes: 1 dead rule `.tt-diff`); Pricing Lab `html:311-530` docs text read, claims not all re-verified; `broker-options-chain.component.ts` only around the QuantLib call; Backend read only at `Query.cs` (QuantLib, comparison, chain snapshot), `PolygonService.cs` (QuantLib methods), DTOs, `Program.cs:53`; `polygon_client.py` only `list_snapshot_options_chain` + snapshot router; FRED/dividend services not read; `strategy-builder`, `black-scholes.ts` internals, `shared/timestamp/*` not read (F1/shared). Untested risk: lightweight-charts with fractional "time" for low-priced underlyings (spot axis uses spot as `UTCTimestamp`).
