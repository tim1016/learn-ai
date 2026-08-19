# Temporal compliance across the live Alpaca path

**Date:** 2026-08-18

**Baseline:** `deb9764f9006cb670abc64d8707d2145ccdd4029` (`origin/master`)

**Charter:** [#1643](https://github.com/tim1016/learn-ai/issues/1643)

**Authority:** `.claude/rules/temporal-rigor.md`, ADR 0022, ADR 0029

**Scope:** `PythonDataService/app/`, `Backend/`, and `Frontend/src/`; the
live-path conclusion is limited to Alpaca Broker V2 control, execution, and
SQLite custody. This is diagnosis only. No temporal field was migrated.

## Result

The live Alpaca contract is substantially cleaner than the repository-wide
grep suggests. Its Pydantic models, persisted SQLite records, generated
OpenAPI types, and V2 UI use `int64 ms UTC`; the V2 UI's only production
`new Date(...)` call receives a number. None of the repository's 55 banned
string/`DateTime` field declarations is on that path.

Four live findings require tracked closure after adversarial refutation:

1. **The live strategy boundary carries native datetimes.** The EMA adapter
   converts numeric market bars into `TradeBar.time` / `end_time` datetimes,
   returns them, stores `end_time` on `StrategyContext`, and passes them through
   strategy/consolidator boundaries. That violates the rule that native time is
   single-function arithmetic only, even though the surrounding wire and
   persistence contracts are numeric.
2. **No real-time liveness fact reaches an operational gate.** The Alpaca path
   has scheduled session state and two channel-health facts, but no real-time
   market-liveness fact at Start, in the V2 market pulse, or at the automated
   strategy's Clerk order-effect boundary. Alpaca `/v2/clock` `is_open` is a
   mapped but read-only market-wide input; no per-symbol trading-status/halt
   input exists. A scheduled-RTH instant during a halt can therefore be
   rendered as `OPEN`, make a stale feed "expected", and reach a bar-driven
   entry effect without independent live-status evidence.
3. **The no-capability fallback invents extended sessions.** The live
   `services/session_authority.py` fallback labels `04:00-09:30` as `PRE` and
   scheduled close-to-`20:00` as `POST`, despite ADR 0029 explicitly requiring
   extended-session structure to come from an IBKR capability snapshot. With no
   capability, the NYSE calendar can prove RTH or closed; it cannot prove those
   instrument/account-specific extended windows.
4. **Deployment validation has an unresolved authority conflict.** Its `09:45`
   detection start and `15:45` stop/flatten are consumed directly by Alpaca V2
   and never adapt to a 13:00 early close. The committed QC reference and its
   provenance note explicitly define those absolute clocks, while the temporal
   rule bans hardcoded session logic. The code therefore cannot be changed to
   relative offsets without first resolving which contract governs half-days.

These are four independently testable seams: in-flight bar representation,
live liveness, scheduled extended-session structure, and a strategy's undefined
half-day contract. No new system ADR is warranted. The temporal rule already
requires numeric in-flight values; ADR 0022 separates live liveness from
scheduled structure; ADR 0029 assigns extended structure to capability
evidence. The strategy-specific absolute-vs-relative choice must be made in its
issue and reference note before implementation.

The rest of the measured debt is outside the live Alpaca path: active research,
Data Lab, portfolio, and LEAN surfaces contain timestamp-string contracts and
string parsing; ten active non-live mechanisms still encode session structure
as constants. One further representation/session literal lives in the retiring
ADR-0038 run ledger. They are recorded here and in `docs/known-gaps.md` so their
counts are neither mistaken for live Alpaca risk nor stranded in this
point-in-time audit.

## Method and measured counts

I ran each ban-list item as a textual grep, then used Python 3.12 and the
repository's TypeScript compiler to classify executable syntax and typed
fields. Counts are source occurrences, not distinct runtime values. Comments,
test-only calls, aliases, and duplicate public/raw interfaces remain in the raw
count and are separated below.

| Ban-list item | Raw / structured count | Classification |
|---|---:|---|
| `datetime.utcnow` | 2 textual | Both are explanatory mentions in `utils/timestamps.py`; 0 executable. |
| `datetime.utcfromtimestamp` | 0 | Clean. |
| `datetime.now()` with no timezone | 0 banned calls | AST found 34 `datetime.now` calls; all pass `UTC`, `tz=UTC`, or another explicit zone. |
| `pd.to_datetime(...)` without `utc=True` | 51 textual; 49 executable; 1 non-UTC | The one is an explicit, bounded QC wall-clock import in `research/parity/fixture_data_reader.py`; 48 executable calls pass `utc=True`. |
| `DateTime.Parse(` | 0 | Clean. Numeric conversion sites use `DateTimeOffset.FromUnixTimeMilliseconds`. |
| `new Date(...)` | 131 textual; 125 executable | 95 take no argument, numeric parts, numbers, or an existing `Date`; 30 take a string/unknown. Of those, 7 are read-confirmed offset-bearing and 23 use non-offset strings (18 production, 5 tests). None of the 23 is in Alpaca V2. |
| Banned temporal field type | 55 declarations | 3 Pydantic, 29 C#, 23 TS. One TS `time` is prose rather than a temporal value; two Python declarations are unused request models. None is on Alpaca V2. |
| `time(9, 30)` / `time(16, 0)` and equivalents | 0 exact call forms; 65 literal clock-constructor/replacement sites; 20 confirmed executable hardcode sites | AST classified all 65: 13 are session hardcodes and 52 are test instants or bounded date arithmetic. A semantic numeric/string pass confirmed 7 more hardcode sites. Four of the 20 are live Alpaca runtime sites, one is retiring, and 15 are active-nonlive/reference sites. |
| `mcal.get_calendar(` | 1 | The sole call is in the canonical `lean_sidecar/trading_calendar.py` module. Clean. |

Commands were rerun from the baseline worktree. Python syntax was parsed with
Python 3.12 because this codebase uses PEP 695 syntax; using the host's older
Python would silently omit files. TypeScript calls and interface properties
were classified from compiler ASTs, not regex alone.

## 1. Representation findings

### 1.1 Live Alpaca wire and persistence contracts are numeric

The evidence is positive, not just the absence of a grep hit:

- broker contract timestamps are numeric (`created_at_ms`, `occurred_at_ms`,
  `submitted_at_ms`, `observed_at_ms`, and the Alpaca clock's
  `vendor_timestamp_ms`) in `broker/contract/models.py:152-317`;
- immutable runner records use numeric creation/start/bind/terminal fields in
  `services/bot_binding_repository.py:84-156`;
- Start evidence uses numeric observation/evaluation fields in
  `schemas/run_admission.py:26-144`;
- the SQLite Clerk models consistently use `*_at_ms: int`, for example
  `broker/alpaca/clerk/sqlite/models.py:28-288`;
- generated Angular broker contracts expose those values as `number`, for
  example `Frontend/src/app/api/broker.types.ts:9379-11063`;
- the V2 panel's one production `new Date(...)` converts chart-library seconds
  back to numeric milliseconds (`dual-pane-chart.component.ts:88`).

The Frontend routes `/api/brokers/...` directly to the Python data plane
(`Frontend/proxy.conf.js:160-161`), so the legacy .NET `DateTime` model set is
not silently interposed on this path.

**Adversarial refutation.** I searched `broker/alpaca/`, the bot binding,
admission, runner, V2 schemas, generated broker types, and V2 components for a
string/`DateTime` temporal field and a string-valued `new Date`. The only
apparent Pydantic result in the broader router scan was the unrelated golden
fixture catalog. The two V2 `new Date` AST nodes are the numeric production
chart conversion and its numeric test. The allegation that the live Alpaca
wire/storage contract still carries the repository's old ISO-string funnel is
therefore refuted. That narrower result does not excuse the in-flight native
engine boundary below.

### 1.2 Live strategy bars violate the numeric in-flight contract

`services/bot_trade_strategy.py::_engine_bar` receives numeric
`MarketDataBar.start_ms` / `end_ms`, converts them to native New York
`datetime` values, places those values in `TradeBar.time` / `end_time`, and
returns the object. The long-lived EMA loop then stores `bar.end_time` on
`StrategyContext.current_time` and passes the bar through the algorithm and its
consolidators. `engine/data/trade_bar.py` declares both fields as `datetime`, so
the value crosses several function/object boundaries and is not a transient
local-arithmetic exception.

This is live on the registered Alpaca EMA strategy path. It is not a wire
serialization defect, but `.claude/rules/temporal-rigor.md` explicitly governs
values “in flight” and permits a native datetime only inside one function. The
surrounding market feed already provides the correct numeric representation;
the adapter currently throws that authority away and recreates it.

The correction must deepen the canonical engine bar contract rather than add a
live-only twin: numeric start/end milliseconds cross strategy, context, and
consolidator boundaries; a function that needs exchange-local wall-clock
arithmetic may create a timezone-aware value locally and return numeric ms.
This is filed as [#1674](https://github.com/tim1016/learn-ai/issues/1674), with
EMA parity required so a representation migration cannot silently change math.

### 1.3 Repository-wide banned field inventory (not live Alpaca)

The 55 declaration hits classify as follows.

| Layer | Count | Read-confirmed classification |
|---|---:|---|
| Pydantic | 3 | `golden_fixtures.ValidationSummary.generated_at` is an active ISO wire value. `TradeRequest.timestamp` and `IndicatorRequest.timestamp` are string fields with no importer/caller beyond their definitions. |
| C# | 29 | 28 `DateTime` fields plus `PolygonResponses.TradeData.Timestamp: string`. They belong to Data Lab, market-data, portfolio, validation, and research DTO/entity/GraphQL surfaces. They are active non-Alpaca-v2 debt; EF persists many of them as `DateTime`, and GraphQL exposes `DateTime!` (for example `PortfolioSnapshot.timestamp`). |
| TypeScript | 23 | 22 real string temporal values across generated validation/backtest types, portfolio GraphQL, Data Lab sessions, jobs, a CSV row, and mock edge data. `WorkedExampleRow.time` is explanatory table prose and is a semantic false positive. Several declarations mirror the same C#/Python wire value and are counted because the contract is duplicated at each layer. |

Exact declaration groups (property multiplicity in parentheses) make the 55
reproducible:

| Layer | Files / fields |
|---|---|
| Pydantic (3) | `models/requests.py`: `TradeRequest.timestamp`, `IndicatorRequest.timestamp` (2); `routers/golden_fixtures.py`: `ValidationSummary.generated_at` (1). |
| C# (29) | `ISnapshotService.cs`: `DrawdownPoint.Timestamp` (1); `DataLabSession.cs`: created/updated (2); portfolio `Account`, `Position`, `PositionLot`, `Order`, `PortfolioSnapshot`, `ValidationResult` (10); market-data `StrategyExecution`, `StockAggregate`, `Ticker`, `Trade`, `OptionsIvSnapshot`, `SignalExperiment`, `TechnicalIndicator`, `ReferenceData`, `Quote`, `ResearchExperiment` (15); `PolygonResponses/TradeData.cs`: string `Timestamp` (1). |
| TypeScript (23) | `jobs.service.ts` (2); `golden-fixtures.types.ts` (1); `data-lab-session.service.ts` (6); `graphql/portfolio-types.ts` (9); generated `broker.types.ts` (`force_flat_at`, `generated_at`) (2); Indicator Report CSV `time` (1); Edge mock `ts` (1); LEAN docs prose `time` (1 false positive). |

The C# model cluster is not a false positive merely because `DateTime.UtcNow`
is UTC-aware: the rule bans the *storage/wire type*, not only naive values.
Conversely, `SnapshotService.ComputeDrawdownSeries` has a local
`(DateTime Timestamp, decimal Equity)` tuple in addition to the 29 declarations;
it is transient arithmetic and not a DTO field, so it is allowed by the stated
ban and excluded from the count.

The generated `EngineBacktestRequest.force_flat_at: string` is active
non-Alpaca backtest debt, not the retiring ledger occurrence discussed in §4.
They share a name and wall-clock representation but have different reachability;
neither is smuggled into the Alpaca V2 binding contract.

### 1.4 `new Date(string)` inventory (not live Alpaca)

The 125 executable constructor calls split into 23 zero-argument, 37 numeric
component, 28 numeric instant, 7 existing-`Date`, and 30 string/unknown calls.

- **7 parse-safe strings:** two Data Lab strings append `Z`; one test literal
  carries `-04:00`; two portfolio values arrive through Hot Chocolate's
  offset-bearing `DateTime` scalar; Data Lab's `updatedAt` is the same scalar;
  LEAN insight times come from `datetime.isoformat()` on engine bar times with
  an explicit zone. These are still implicated in the separate banned-field
  declarations, but they do not meet this specific parser ban.
- **18 production violations:** date-only or local-wall strings in date
  validation (1), the option-expiry ribbon (2), Options Lab (2), Strategy
  Builder (1), ticker-range helpers (2), Data Lab ranges (5), Past Chain (1),
  Indicator Report CSV ingestion (1), Market Calendar (1), and Research
  Feature Report (2). Typical examples append `T00:00:00` without an offset
  (`expiration-ribbon.component.ts:32,57`) or pass `YYYY-MM-DD` directly
  (`ticker-range-picker.types.ts:136`).
- **5 test-only violations:** ticker-range component tests pass date-only form
  values directly to `new Date`.

**Adversarial refutation.** Numeric `UTCTimestamp` chart conversions were not
counted as string parsing. The portfolio/Data Lab display calls were traced
back to GraphQL `DateTime` rather than condemned by variable name. The
remaining 23 have either a literal non-offset construction or a date-only
interface/caller; no upstream offset guarantee exists.

### 1.5 Pandas non-UTC candidate is a bounded wall-clock ingestion exception

`research/parity/fixture_data_reader.py:100` deliberately parses a QC CSV's
`YYYY-MM-DD HH:MM:SS` as a local wall clock with `utc=False`, then attaches
`America/New_York` at `:116` before constructing engine bars. The source value
does not identify an instant until that zone is applied. It is research-only,
never reaches Alpaca, and its comments explicitly fence the naive value between
the parse and conversion. This apparent violation is refuted for this charter.

The research fixture is not the source of the live violation. The separately
confirmed native `TradeBar` boundary is tracked in §1.2 and #1674.

## 2. Scheduled session structure

### 2.1 Calendar construction is single-authority

The only `mcal.get_calendar(...)` call is
`lean_sidecar/trading_calendar.py:29`. Consumers import the canonical helpers.
The former multi-calendar condition described by ADR 0022 is therefore closed.

### 2.2 Equivalent-literal census

The exact `time(9, 30)` / `time(16, 0)` call forms are absent. The broader
review deliberately did not stop there.

Python ASTs contain 65 calls that construct or replace a literal wall clock:
22 `time(...)`, 31 `datetime(...)`, and 12 `.replace(hour=...)` calls. Thirteen
are confirmed session hardcodes. The other 52 are 44 test instants plus eight
production-local midnight/end-of-date conversions; they do not author a market
boundary. A second semantic scan for minute counts, hour comparisons, offsets,
and clock strings confirmed seven more executable sites. The resulting 20 are:

| Reachability | Confirmed source occurrences | Classification |
|---|---|---|
| **Live Alpaca** | `services/session_authority.py:15-16`; `engine/strategy/algorithms/deployment_validation.py:31-32` | Two live scheduled-time findings, detailed below. |
| **Retiring ADR 0038** | `engine/live/config.py:59` | Default 15:55 close barrier, also serialized as a string in the retiring run ledger. Delete with that plane; do not migrate it. |
| Reference/parity copy | `data/qc-shadow/DeploymentValidationAlgorithm.py:27-28` | Same fixed deployment-validation schedule. It is not imported by the runtime, but must change with the canonical strategy so the claimed twin does not lie. The generated trusted-template source repeats the same constants as text. |
| Canonical calendar / LEAN | `lean_sidecar/trading_calendar.py:31,156`; `routers/lean_sidecar.py:137`; `services/lean_sidecar_service.py:323` | Hardcoded regular-close classification, non-session 09:30 fallback, 09:30 request validation, and a 09:30 synthetic sample. Active, not Alpaca V2. |
| Data Lab/chart | `services/chart_service.py:193,408,476-477,551`; `services/dataset_service.py:731` | Fixed 04:00/20:00 extended coverage, fixed 960-minute expectation, fixed `:30` RTH resample origin, and fixed 20:00 post tag. Active, not Alpaca V2. |
| Research/data quality | `engine/edge/features_realtime/hf_realized_vol.py:71`; `services/data_quality_service.py:124`; `Backend/Services/Implementation/MarketDataService.cs:353` | Fixed 04:00-20:00 ETH mask and two 390-minute normal-day assumptions. Active, not Alpaca V2. |

The active non-live sites are real temporal debt: for example the .NET quality
check counts weekdays rather than sessions and treats 390 bars as universal,
while the Python quality check discovers early closes by comparing calendar
duration with the same constant. They are outside this charter's live-Alpaca
question, so no Alpaca implementation issue is drafted; the active clusters
are nevertheless registered durably in `docs/known-gaps.md` §6.

### 2.3 Live hardcoded extended-session fallback

`services/session_authority.py:15-16,109-153` combines the canonical day's RTH
window with local `04:00` and `20:00` constants. In the absence of a capability
snapshot, it returns `PRE` and `POST`, assigns `source="nyse_calendar"`, and can
set `permits_strategy_activity=True` for an extended-session allow-list.

That is the exact behavior ADR 0029 rejected: the operator surface must not
derive PRE/POST from local hardcoded windows. Its fallback promise is narrower:
preserve calendar-backed RTH behavior when capability evidence is missing.
`bot_start_admission.py` and `broker_v2_panel/market_pulse.py` call this function
without a capability, so the defect is live rather than merely latent in the
older engine.

**Adversarial refutation failed.** The submit mechanism remains RTH-only today,
which prevents this fallback alone from enabling an extended Alpaca order. It
does not cure the false phase: the V2 panel and Start evidence still present/use
invented extended structure, and a future mechanism flag would turn that lie
into execution policy. The smallest correction is to make no-capability
fallback RTH-or-closed only and thread real capability evidence to consumers
that need PRE/POST/OVERNIGHT.

### 2.4 Live deployment-validation cutoffs leave half-days undefined

The canonical `DeploymentValidationDecisionKernel` compares every live bar
against `time(9, 45)` and `time(15, 45)`
(`engine/strategy/algorithms/deployment_validation.py:31-32,92-108`). Alpaca V2
imports that kernel directly (`services/bot_trade_strategy.py:25-28,99-123`).
On a normal day those clocks happen to be 15 minutes after the scheduled open
and 15 minutes before the scheduled close. On a 13:00 half-day, the kernel can
emit an entry near the close and never sees a 15:45 bar on which to emit its
intended flatten.

This finding cannot be “fixed” by assuming the clocks were intended as relative
offsets. `references/qc-shadow/DeploymentValidationAlgorithm.py` and
`docs/references/deployment-validation-consecutive-green.md` explicitly define
absolute 09:45/15:45 ET. Under the repository hierarchy that committed
reference/provenance is ground truth for the current strategy, while
`.claude/rules/temporal-rigor.md` bans hardcoded session logic and requires the
calendar for scheduled boundaries. The conflict is read-confirmed and must be
surfaced, not silently resolved.

**Adversarial refutation partially succeeds, but not enough to close the
finding.** The constants are genuine strategy policy, not an attempt to
reimplement the NYSE open/close, so fixed 09:45 by itself need not be relabeled
as a calendar. But the `STOP_AND_FLATTEN` safety behavior depends on receiving a
bar that cannot exist after an early close, and the strategy does not consult
the canonical session window to bound entry or flattening. A tracked issue must
decide the half-day contract, preserve or deliberately supersede the reference,
and prove the chosen rule on both engines.

### 2.5 Apparent literals that are not session-authority defects

- `options_companion_service.py:69,394` and
  `Backend/Services/Implementation/PortfolioRiskService.cs:432-438` anchor
  option expiry to `16:00 ET`, exactly the rule's required date anchor.
- `research/ml/generators/quantconnect_fixture.py:102` records a configurable
  reference-export anchor and emits ms UTC; it does not claim an exchange
  boundary.
- `services/iv_recorder.py:50` uses `16:00` as one experimental measurement
  slot, not as a market-open verdict.
- Frontend/Data Lab `390` multipliers and the research engine's `390 * 252`
  values are row-count estimates or explicit annualization conventions. They
  do not filter a timestamp or answer a scheduled-session question.
- The 44 literal clocks in tests are fixture instants or configurable-cutoff
  inputs. They do not become an authority merely because a clock resembles a
  market time.

## 3. Real-time liveness — confirmed live defect

### 3.1 Scheduled phase is used as if it were live truth

ADR 0022 draws a two-axis boundary: the calendar owns scheduled structure; the
live broker/vendor feed wins for an operational "open this second" decision.
ADR 0029 adds IBKR capability windows for scheduled extended-session structure
and explicitly permits a calendar fallback for *phase*. Those decisions are
compatible.

Current code collapses the axes:

- `bot_start_admission.py:324-335` calls scheduled
  `session_state_at_ms(...)` to decide whether a stale feed should block Start;
  Resume reuses the same fact (`bot_resume_admission.py:118`).
- `broker_v2_panel/market_pulse.py:24-61` maps that scheduled phase directly to
  the operator label `OPEN` / `CLOSED` and to whether missing bars need
  attention.
- the actual Alpaca runner's trade path calls
  `clerk.execute_for_instance(...)` from `bot_trade_strategy.py:196-267` with no
  market-liveness fact. The Clerk's production gate combines IBKR market-data
  connectivity/staleness and Alpaca `trade_updates` health only
  (`clerk/stream_health.py:1-24,117-188`).
- a connected feed with no active subscription is deliberately classified
  available (`tests/services/test_bot_start_admission.py:367-389`), so that
  path supplies no positive market-open evidence before Start.

A partial input already exists at ingestion: Alpaca `/v2/clock` maps `is_open`,
`vendor_timestamp_ms`, and `observed_at_ms` into `BrokerClockEvidence`
(`broker/alpaca/adapter.py:482-496`; contract at
`broker/contract/models.py:302-317`). A repo-wide caller search finds it only in
the broker read port and `/api/brokers/{broker}/clock` route
(`routers/brokers.py:294-299`), not in Start, panel projection, or the Clerk.
The model/test wording that vendor evidence is "never authority" overreaches ADR
0032: it must not replace the calendar for *scheduled structure*, but it can
author the market-wide live-clock observation within its stated scope.

It is not sufficient for halt safety. Alpaca documents the clock as answering
whether the market is currently open; its stock-data trading-status messages
are the separate symbol-scoped source that identifies halts and resumes. Alpaca
also documents that an order for a halted security can be accepted and held,
so broker acceptance is not a liveness oracle. See the official
[market-clock reference](https://docs.alpaca.markets/us/reference/legacyclock),
[stock trading-status stream](https://docs.alpaca.markets/us/v1.1/docs/real-time-stock-pricing-data),
and [halted-order behavior](https://docs.alpaca.markets/us/docs/245-trading-for-trading-api).

### 3.2 Why the existing health gates do not refute the finding

- **Connected is not open.** A healthy IBKR socket and healthy Alpaca
  `trade_updates` socket say the channels work, not that the market accepts a
  new order now.
- **Bar freshness is delayed evidence.** It eventually fails closed after a
  halt, but it admits a grace window after the last bar and deliberately has no
  proof when there is no candidate subscription.
- **IBKR capability is scheduled evidence.** Its persisted trading/liquid-hours
  windows implement ADR 0029; they cannot know an unscheduled halt and do not
  satisfy ADR 0022's live axis.
- **Neither live source is already consumed indirectly.**
  `get_clock_evidence` has no caller in the live mutation path, and the FastAPI
  route is read-only. The app has no Alpaca symbol trading-status/halt model or
  subscriber; `trade_updates` carries this account's order events, not the
  market-data status messages documented above.
- **Manual order submission is not proof of the same defect.** The SQLite
  manual preview rechecks account eligibility, asset eligibility, custody, and
  channel health before `manual_orders.py` calls `trade.submit`, but it does not
  ask whether the market is open (`manual_order_runtime.py:120-447`;
  `manual_orders.py:476-535`). That absence was investigated rather than
  silently folded into this finding. The manual endpoint does not claim an
  `OPEN` state, and its explicit `DAY`/`GTC` order contract permits an operator
  to place an order for broker-side queuing while the market is closed. Alpaca
  likewise documents that a DAY order submitted after close is queued for the
  following trading day in its
  [order reference](https://docs.alpaca.markets/us/v1.4.2/reference/replaceorderforaccount).
  The temporal rule chooses the authority *when an open-now answer is required*;
  it does not independently impose a trading-hours policy. Manual entry should
  therefore remain outside the implementation issue unless a separate product
  decision says it must be RTH-only.
- **This is not the deprecated IBKR control plane.** `BotTaskRegistry` and
  `run_trade_bot` are the Alpaca Broker V2 runner/Clerk path. The fix must not
  be routed through `live_instances.py` or `BotLifecycleEvaluator`.

A scheduled-RTH / live-closed-or-halted divergence therefore remains
observable and actionable. For new exposure, the current path can discover it
only after data becomes stale; even broker acceptance can leave the order held
through a halt. That is the failure mode the authority rule forbids.

## 4. Retiring paths

ADR-0037 legacy-JSONL custody itself contributed no confirmed hit: matching
Pandas calls pass `utc=True`, custody evidence uses `*_ms` integers, and
`rollup_cache.py` reads the canonical calendar window. The deprecated Angular
IBKR bot-control folders also contain no string-valued `new Date(...)`
candidate. `live_portfolio.liquidate(symbol, time: datetime)` is an internal
engine parameter rather than a DTO/wire/storage field and is allowed transient
arithmetic.

The ADR-0038 evaluator/run-ledger plane does contain one confirmed temporal
violation hidden from the typed-field grep by `live_config: dict`:

- `LiveConfig.force_flat_at` defaults to `time(15, 55)` specifically to target
  the normal 16:00 close (`engine/live/config.py:48-60`);
- the deploy boundary accepts the broad dictionary and persists it in
  `run_ledger.json` (`schemas/live_runs.py:665-702`);
- `_live_config_from_ledger` requires `force_flat_at` to be an `HH:MM` or
  `HH:MM:SS` string and converts it back to `time` (`engine/live/run.py:986-1001`).

That violates both representation-at-rest and the no-hardcoded-session rule,
but ADR 0038 Decision 1 / Consequence 6 already retires this run ledger with
the IBKR evaluator plane. The resolution is deletion, not an `int64` migration.
No standalone temporal issue is proposed while that retirement remains parked.

## 5. Filed live issues

### 5.1 Filed issue [#1671](https://github.com/tim1016/learn-ai/issues/1671): add scoped live-liveness evidence

#### Title

Live Alpaca: separate real-time market liveness from scheduled session phase

#### Body

**Source:** temporal-compliance charter #1643 and
`docs/audits/temporal-compliance-2026-08-18.md`.

##### Problem

ADR 0022 and `.claude/rules/temporal-rigor.md` require two distinct truths:

- the canonical calendar / ADR-0029 capability windows own scheduled session
  structure;
- a live broker/vendor signal owns whether the market is open this second and
  wins for operational decisions.

Alpaca V2 currently has no shared live-liveness fact at its Start, panel, or
Clerk effect boundaries. `market_data_admission_fact` and the V2 market pulse
derive bar expectation / `OPEN` from scheduled `session_state_at_ms`.
`run_trade_bot` reaches `clerk.execute_for_instance` behind market-data and
`trade_updates` channel health, but no broker-open fact. Alpaca's mapped
`BrokerClockEvidence.is_open` is exposed only by the read endpoint, and the app
does not ingest the symbol-scoped trading-status messages that carry halts and
resumes.

This permits a scheduled-RTH / vendor-closed divergence (halt, emergency
closure, or transition race) to remain `OPEN` until a stale timeout. A connected
idle feed with no subscription is deliberately `AVAILABLE`, so it is not
positive liveness proof. Alpaca documents that a halted-symbol order can be
accepted and held, so an accepted submit does not close this evidence gap.

##### Required outcome

Introduce one typed, freshness-bounded, symbol-scoped `MarketLivenessFact`
(numeric ms clocks, explicit sources, `TRADABLE | HALTED | CLOSED | UNKNOWN`).
Compose market-wide clock state with the appropriate symbol trading-status
source; do not pretend the existing clock alone proves a security is not
halted. Keep scheduled phase as a separate value, do not replace or weaken the
canonical calendar, and do not route this through the retiring IBKR evaluator
plane.

##### Acceptance criteria

1. Start/Resume evidence and the V2 market pulse consume the same liveness fact
   while retaining calendar/capability phase separately.
2. Immediately before a bar-driven automated-strategy Clerk effect that can
   create new exposure, fresh `TRADABLE` evidence for that symbol is required.
   `HALTED`, `CLOSED`, `UNKNOWN`, stale, or unavailable evidence fails closed
   with a typed reason and durable decision receipt.
3. Automated exit/cancellation behavior is specified explicitly and pinned by
   tests; the new gate must not accidentally prevent emergency risk reduction
   merely because entry is closed. Manual-order policy is unchanged: do not
   add an RTH-only gate to manual `DAY`/`GTC` submission without a separate
   product decision.
4. Regression tests force calendar/capability phase `RTH` with (a) market-wide
   live state `CLOSED` and (b) market-wide `OPEN` plus symbol state `HALTED`.
   Assert the panel does not claim tradability, Start does not claim proven
   liveness, and no new-exposure broker submit occurs. A third case pins
   missing/stale evidence to `UNKNOWN` and fail-closed behavior.
5. A parity test preserves the other axis: when scheduled phase and fresh live
   evidence agree, existing RTH behavior is unchanged; historical/session
   queries remain calendar-owned.
6. Correct `BrokerClockEvidence` model/adapter test comments to state its narrow
   role: evidence only, never scheduled-session authority; authoritative input
   for market-wide live-clock state, but not proof against a symbol halt.
7. All new/changed temporal fields remain `int64 ms UTC`; no ISO/string or
   native datetime crosses a wire/storage boundary.

##### Verification seam

Use fake clock and symbol-status evidence plus the existing Start-admission,
`broker/v2panel/test_market_pulse.py`, and SQLite Clerk effect tests. Assert the
trade port is never invoked in the divergence case. No live broker call is
required in CI.

### 5.2 Filed issue [#1673](https://github.com/tim1016/learn-ai/issues/1673): remove the invented extended-session fallback

#### Title

Alpaca V2: require capability evidence for extended-session phase

#### Body

**Source:** temporal-compliance charter #1643 and
`docs/audits/temporal-compliance-2026-08-18.md`.

##### Problem

ADR 0029 says IBKR capability snapshots own live per-instrument extended
structure and explicitly rejects deriving PRE/POST from hardcoded local clock
windows. When capability is unavailable, only existing calendar-backed RTH
behavior is preserved.

`services/session_authority.py` nevertheless combines the calendar RTH window
with `_PRE_OPEN = time(4, 0)` and `_POST_CLOSE = time(20, 0)`. Its fallback can
return `PRE`/`POST`, advertise `source="nyse_calendar"`, and permit extended
strategy activity. Alpaca V2 Start and market-pulse callers do not pass a
capability, so they consume this fallback in production.

The RTH-only order mechanism currently prevents this false phase from placing
an extended-hours order, but the operator/admission facts are already wrong and
the latent submit risk appears as soon as extended placement is enabled.

##### Required outcome

Make scheduled phase provenance truthful. Capability evidence may author
PRE/POST/OVERNIGHT. Without fresh matching capability evidence, the canonical
NYSE calendar may author only RTH or CLOSED and the next canonical RTH
transition; it must not synthesize an extended window.

##### Acceptance criteria

1. Delete `_PRE_OPEN` / `_POST_CLOSE` and all fixed 04:00/20:00 fallback logic
   from `services/session_authority.py`.
2. A missing, malformed, mismatched, or stale capability produces only
   calendar-backed `RTH`/`CLOSED`; it never produces PRE/POST/OVERNIGHT or
   `permits_strategy_activity=True` for an extended-only policy.
3. Start/Resume and V2 market-pulse orchestration receive a fresh matching
   capability when they need extended phase. If no such evidence exists, their
   typed evidence and operator copy say that extended phase is unproved rather
   than inventing it.
4. Tests cover before-open, after-close, a 13:00 early close, weekend/holiday,
   stale capability, and a valid instrument/account-matching capability.
5. Existing regular-day RTH behavior and `int64 ms UTC` boundaries remain
   unchanged. A parity test names the canonical calendar module for the RTH
   overlap.
6. Do not route the correction through the retiring IBKR evaluator plane. IBKR
   remains an allowed market-data/capability source under ADRs 0032/0029, not a
   bot-control authority.

##### Verification seam

Extend `tests/services/test_session_authority.py`, Start-admission tests, and
`tests/services/broker/v2panel/test_market_pulse.py` with deterministic
capability fixtures. No live broker call is required.

### 5.3 Filed issue [#1672](https://github.com/tim1016/learn-ai/issues/1672): resolve deployment-validation half-day policy

#### Title

Live deployment validation: define and enforce the half-day cutoff contract

#### Body

**Source:** temporal-compliance charter #1643 and
`docs/audits/temporal-compliance-2026-08-18.md`.

##### Problem

The canonical `DeploymentValidationDecisionKernel`, which Alpaca V2 imports
directly, hardcodes `_DETECTION_START = time(9, 45)` and
`_STOP_AND_FLATTEN = time(15, 45)`. On a 13:00 session the kernel never sees its
15:45 stop, so a late half-day entry can survive without the intended flatten
signal.

This is also an authority conflict, not just an implementation typo. The
committed QC audit copy and
`docs/references/deployment-validation-consecutive-green.md` explicitly define
absolute 09:45/15:45 ET. That reference is the current strategy ground truth;
the temporal rule simultaneously bans hardcoded session logic and requires
calendar-owned boundaries. The QC shadow and LEAN trusted sample repeat the
same constants, so current parity does not define half-day behavior.

##### Required outcome

Make an explicit product/spec decision before changing the kernel:

- **calendar-relative:** start 15 minutes after scheduled open and stop/flatten
  15 minutes before scheduled close; or
- **reference-preserving absolute clocks:** retain 09:45/15:45 as strategy
  policy, but define a separate canonical-calendar half-day entry/flatten clamp
  that cannot wait for a nonexistent 15:45 bar.

Whichever is chosen must preserve regular-day behavior, make half-day safety
total, and deliberately update (not silently reinterpret) the reference and QC
evidence.

##### Acceptance criteria

1. Record the chosen absolute-vs-relative contract and its early-close behavior
   in `docs/references/deployment-validation-consecutive-green.md`; cite that
   decision in the kernel docstring and tests.
2. The Python kernel consumes the current canonical session window as numeric
   ms (or a narrow injected resolver). A known half-day fixture (for example
   2024-11-29) proves no ENTER can be emitted beyond the documented safe cutoff
   and an active cycle emits EXIT before the 13:00 close.
3. A normal-session fixture proves byte-for-behavior parity with current
   09:45/15:45 decisions, including bar-close boundary inclusion.
4. Update the QC shadow and LEAN trusted companion atomically. The LEAN side
   derives exchange hours from its exchange-hours authority (or receives the
   resolved numeric window); a named parity test compares it with
   `lean_sidecar/trading_calendar.py` on regular and half days.
5. If the chosen behavior changes the committed QC reference, produce a new QC
   audit/backtest receipt and retire the superseded evidence explicitly. If
   absolute clocks remain, explain why they are strategy policy rather than a
   second exchange calendar and keep the half-day clamp calendar-owned.
6. All boundary values crossing a process/file/wire remain `int64 ms UTC`.

##### Verification seam

Extend the deployment-validation kernel, live adapter, and trusted-template
tests with a regular day and Black Friday. The live-adapter test should feed
closed numeric-ms bars and assert the semantic intent stream without a broker.
This issue needs the absolute-vs-relative product choice before implementation;
the audit does not make that choice implicitly.

### 5.4 Filed issue [#1674](https://github.com/tim1016/learn-ai/issues/1674): migrate live engine bars to numeric time

#### Title

Live Alpaca: keep engine bar timestamps int64 ms across strategy boundaries

#### Body

**Source:** temporal-compliance charter #1643 and
`docs/audits/temporal-compliance-2026-08-18.md`.

##### Problem

The temporal contract requires every value in flight, at rest, or on the wire
to be `int64 ms UTC`; native datetime objects may exist only for arithmetic
inside one function and must be converted back before return.

The live Alpaca EMA adapter violates that boundary. `_engine_bar` converts
numeric `MarketDataBar.start_ms/end_ms` into a `TradeBar` with two `datetime`
fields, returns it, assigns `StrategyContext.current_time = bar.end_time`, and
passes/stores it through strategy and consolidator boundaries. The canonical
`TradeBar` model declares those datetime fields, so this is not a local
transient conversion.

##### Required outcome

Make the canonical engine/strategy bar-time contract numeric milliseconds. Do
not add a live-only duplicate bar model or preserve a second datetime authority.
Native timezone-aware values may be constructed narrowly inside one function
for wall-clock arithmetic, then converted back before return.

##### Acceptance criteria

1. `TradeBar` or its single canonical successor exposes numeric `start_ms` and
   `end_ms`; no datetime-valued temporal field crosses the live adapter,
   strategy, consolidator, context, event, file, or wire boundary.
2. Migrate `StrategyContext.current_time`, consolidators, algorithms, and all
   affected callers to the numeric contract. Any temporary compatibility
   adapter is fenced to one function and removed by this issue.
3. Preserve period and exchange-local strategy policy by converting numeric ms
   to a timezone-aware native value only inside the function doing local
   arithmetic.
4. Regression tests prove `_engine_bar` returns numeric timestamps, the
   long-lived live strategy context retains no native datetime, and one EMA
   live decision remains numerically identical to the existing LEAN/golden
   receipt.
5. Run the full engine/strategy/live-adapter test scope plus project Ruff.
   Update both math/engine authority maps if canonical locations or consumers
   move.
6. No ISO/string/native datetime field is introduced at any boundary; all
   changed timestamps remain `int64 ms UTC`.

## 6. Registered `docs/known-gaps.md` bullets

- **Live Alpaca conflates scheduled phase with real-time market liveness
  (high).** `services/bot_start_admission.py:324-335` and
  `services/broker_v2_panel/market_pulse.py:24-61` use the calendar/capability
  session phase to decide whether bars are expected and to render `OPEN`, while
  `services/bot_trade_strategy.py:196-267` reaches the Alpaca Clerk without a
  live status fact. The Clerk gate proves IBKR market-data and Alpaca
  `trade_updates` channel health, not market-wide open state or a symbol halt.
  Alpaca's timestamped `BrokerClockEvidence.is_open`
  (`broker/alpaca/adapter.py:482-496`) is a read-endpoint-only partial input,
  while symbol trading-status messages are not ingested. Preserve ADR 0022's
  split: calendar/capability remains scheduled authority; fresh market-wide and
  symbol-scoped evidence wins at Start, operator projection, and the automated
  new-exposure effect gate.
  [#1671](https://github.com/tim1016/learn-ai/issues/1671)

- **Alpaca V2 invents extended-session phase without capability evidence
  (high).** `services/session_authority.py:15-16,109-153` labels fixed
  04:00-09:30 and close-to-20:00 windows as PRE/POST when no matching IBKR
  capability exists, even though ADR 0029 assigns extended structure to that
  capability and permits only calendar-backed RTH fallback. Start and V2 market
  pulse call this path without capability evidence. Remove the constants;
  missing/stale capability must degrade to calendar RTH/CLOSED and an explicitly
  unproved extended phase.
  [#1673](https://github.com/tim1016/learn-ai/issues/1673)

- **Live deployment validation misses early-close flattening (high).**
  `engine/strategy/algorithms/deployment_validation.py:31-32,92-108` hardcodes
  09:45 detection and 15:45 stop/flatten, and
  `services/bot_trade_strategy.py:99-123` feeds that kernel on the Alpaca V2
  runtime. A 13:00 half-day never reaches the stop, so a late entry can outlive
  the intended daily exit. The QC reference explicitly defines absolute clocks,
  so first decide absolute-with-calendar-clamp versus calendar-relative
  cutoffs; then update the reference and pin Python/LEAN parity on regular and
  early-close days.
  [#1672](https://github.com/tim1016/learn-ai/issues/1672)

- **Live Alpaca engine bars carry native datetimes across strategy boundaries
  (high).** `services/bot_trade_strategy.py:80-90,139-146` converts numeric feed
  timestamps into `TradeBar.time` / `end_time`, returns the object, stores its
  end time on `StrategyContext`, and passes it through strategy/consolidator
  objects. `engine/data/trade_bar.py:17-40` makes both fields `datetime`. Migrate
  the one canonical engine bar/context contract to numeric start/end ms and
  preserve EMA/LEAN parity; do not add a live-only duplicate model.
  [#1674](https://github.com/tim1016/learn-ai/issues/1674)

## 7. Closure plan

1. The four live issues are filed and their linked known-gap bullets land with
   this documentation PR.
2. Land the scheduled-authority corrections first: remove the invented
   extended fallback, then resolve and implement deployment-validation's
   absolute-vs-relative half-day contract.
3. Implement the typed liveness fact as a separate vertical slice through
   ingestion, Start/Resume, V2 market pulse, and the automated-strategy Clerk
   effect gate.
4. Prove half-day, scheduled-RTH/vendor-closed, and missing/stale-evidence
   behavior at the named seams. Preserve calendar ownership for historical and
   scheduled questions.
5. Close each issue only after its implementation PR removes its known-gap
   bullet. Do not couple closure to the unrelated Data Lab/portfolio/LEAN
   representation backlog or migrate the retiring run ledger.

No ADR step precedes implementation: the authority choices are already binding
in ADRs 0022 and 0029. The retiring `force_flat_at` violation resolves only with
the separately scoped ADR-0038 deletion; do not spend a migration on it.
