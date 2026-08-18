# Clerk lineage coupling and reachability

**Question.** What does the Alpaca Clerk lineage actually depend on from the
IBKR-era `engine/live/account_clerk*` lineage, and which IBKR-importing routers
are reachable from the live Alpaca UI?

**Source commit.** [`e7325d2ff0a122ffde3418cac94aa2f872f10ffa`](https://github.com/tim1016/learn-ai/tree/e7325d2ff0a122ffde3418cac94aa2f872f10ffa)

**Research date.** 2026-08-17

**Answer.** The Alpaca Clerk has one direct dependency on the old
`account_clerk*` lineage: its JSONL journal reuses
`AccountClerkBrokerEvidenceBaseline` and the nested
`AccountClerkPositionEvidence` from `account_clerk_journal_models.py`. That is a
real runtime dependency for the legacy Alpaca authority's operator-confirmed
inventory-baseline recovery, not merely a type annotation. It is also broader
at import time than the two intended contracts: the source module imports
`IbkrOrderAck` and `IbkrOrderEvent` at module scope, so starting the current
FastAPI application loads IBKR models even when SQLite is the selected Alpaca
authority. The accepted SQLite authority neither constructs those baseline
models nor executes the old Clerk facade/RPC/lease/journal machinery; its
product paths fail closed instead of falling back. Of the nine named modules
that directly import `app.broker.ibkr`, only `routers/broker.py` and its helper
`routers/broker_dependencies.py` are reached at request time from the canonical
Alpaca UI: the global shell polls the IBKR `/api/broker/health` endpoint every
five seconds. Separately, the Alpaca bot signal feed and panel LIVE chart
deliberately execute IBKR bar code, but ADR 0032 limits that bridge to market
data; Alpaca order effects still flow through the Alpaca Clerk. "Live UI" here
means the networked operator UI and on-duty paper bots: real-money Alpaca mode
is rejected at configuration and has no admission or execution path.

This audit is supporting evidence, not implementation authority, consistent
with the [Docs Authority Index](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/docs/doc-authority.md#L3-L17).

## Method and terms

I read the canonical Angular routes and callers, followed each HTTP call through
the FastAPI router and service/engine imports, and then made a second pass from
the suspected backend modules back toward production UI callers. I inspected
all 12 `engine/live/account_clerk*.py` files, all top-level Alpaca Clerk modules,
the SQLite package boundary, the nine direct-IBKR-import candidates named in the
issue, FastAPI registration/startup, the active-authority test code, ADRs 0030,
0032, and 0035, and the current documentation-authority index. This was static
analysis, including reading focused tests; no test or live-broker exercise was
run.

The table distinguishes three kinds of reachability:

- **Executed** means a function in the module runs while serving the stated UI
  action.
- **Loaded** means Python evaluates imports/class definitions at FastAPI startup,
  but the request does not call the lineage's behavior.
- **Conditional** means the route is canonical, but authority selection decides
  whether the legacy JSONL or accepted SQLite implementation executes. A valid
  activation selects SQLite; no activation selects legacy; an invalid or failed
  activation becomes unavailable and does not fall back
  ([selector](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/PythonDataService/app/broker/alpaca/clerk/active_authority.py#L150-L237),
  [SQLite branch](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/PythonDataService/app/broker/alpaca/clerk/active_authority.py#L239-L319)).

## Reachability map

| Module | Lineage (alpaca / ibkr / shared) | On a live Alpaca request path? | Entry point that reaches it | Shared symbols | Notes |
|---|---|---|---|---|---|
| `Frontend/src/app/app.component.ts` → `services/broker-health.service.ts` | shared shell, IBKR health contract | **Yes — executed on every route** | App startup renders the global banner and starts immediate/5-second polling ([shell](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/Frontend/src/app/app.component.ts#L71-L113), [poll](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/Frontend/src/app/services/broker-health.service.ts#L78-L105)) | `IbkrConnectionHealth` | Therefore `/brokers/alpaca` does not isolate the browser from the IBKR control surface. |
| `routers/broker.py` → `routers/broker_dependencies.py` → `broker/ibkr/*` | ibkr | **Yes — executed** | `BrokerService.health()` calls `GET /api/broker/health` ([client](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/Frontend/src/app/services/broker.service.ts#L79-L87)); the endpoint calls `get_settings`, `is_broker_disabled`, `get_client`, monitor, and IBKR health builders ([endpoint](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/PythonDataService/app/routers/broker.py#L215-L255), [helper](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/PythonDataService/app/routers/broker_dependencies.py#L7-L18)) | None | This is a live Alpaca **UI → IBKR code** path, but not an Alpaca order-execution path and not old Account Clerk behavior. |
| `routers/brokers.py` → `broker/alpaca/clerk/__init__.py` → `clerk.py`, `models.py` | alpaca, with shared model dependency | **Loaded always; executed only for legacy JSONL authority** | Canonical Alpaca account, Clerk, custody and action routes import the package ([router import](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/PythonDataService/app/routers/brokers.py#L21-L35)); the package eagerly imports the JSONL Clerk ([package](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/PythonDataService/app/broker/alpaca/clerk/__init__.py#L20-L27)) | `AccountClerkBrokerEvidenceBaseline`; `AccountClerkPositionEvidence` | A valid SQLite activation uses the SQLite facade; eager imports still load the JSONL classes. |
| `broker/alpaca/clerk/clerk.py`, `custody_resolution.py`, `models.py` | alpaca legacy JSONL + shared evidence contract | **Conditional — executed on legacy inventory-baseline recovery; rejected on active SQLite** | Alpaca desk custody resolve or panel recovery action can reach `_record_inventory_baseline_locked` ([resolve](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/PythonDataService/app/broker/alpaca/clerk/custody_resolution.py#L278-L300)); it constructs both shared classes and appends the baseline ([construction](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/PythonDataService/app/broker/alpaca/clerk/clerk.py#L814-L836)). Active SQLite rejects the generic action before invoking the JSONL Clerk ([guard](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/PythonDataService/app/routers/brokers.py#L777-L825)). | Both named evidence models | The semantic reason is an account-level, namespace-free fresh broker snapshot. ADR 0030 specifies that recovery record and its safeguards ([amendment](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/docs/architecture/adrs/0030-account-clerk-account-rooted-journal.md#L394-L414)). |
| Other top-level `broker/alpaca/clerk/*.py` modules | alpaca; a few broker-neutral helpers are reused by SQLite | **Conditional** | Legacy JSONL submit/cancel/reconcile/custody requests use the JSONL implementation. Active SQLite reuses small Alpaca helpers such as `DecisionOutcome`, `FillRecord`, `StreamHealthGate`, terminal-order constants, and `UNCERTAIN_SUBMIT_GRACE_MS`; it does not use old `engine/live/account_clerk*` behavior. | No additional `engine/live/account_clerk*` symbol | These are Alpaca modules despite some legacy-JSONL mechanics. SQLite explicitly imports selected helpers rather than the IBKR Clerk. |
| `broker/alpaca/clerk/active_authority.py`, `active_protocol.py`, `sqlite/*.py` | alpaca | **Yes — executed for an activated account** | Canonical broker/account, bot panel, manual order, Clerk command, transaction, and projection routes use the selected SQLite facade/repository. | None from `engine/live/account_clerk*` | ADR 0035 makes schema-v8 SQLite folds the active product authority and bars JSONL/Postgres product fallback ([authority scope](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/docs/architecture/adrs/0035-alpaca-clerk-sqlite-event-sourced-authority.md#L192-L202)). SQLite's `runtime.py` imports Alpaca `models.py`, so it indirectly loads the old evidence-model module, but it does not construct its baseline classes. |
| `engine/live/account_clerk_journal_models.py` | **shared contract module with IBKR payloads** | **Loaded on active SQLite; two classes executed only on legacy Alpaca baseline recovery** | Imported directly by Alpaca `clerk.py` and `models.py` ([imports](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/PythonDataService/app/broker/alpaca/clerk/clerk.py#L85-L88), [field](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/PythonDataService/app/broker/alpaca/clerk/models.py#L21-L22)). | `AccountClerkBrokerEvidenceBaseline`; `AccountClerkPositionEvidence` | The two classes themselves contain no IBKR types ([definitions](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/PythonDataService/app/engine/live/account_clerk_journal_models.py#L97-L120)), but their module imports IBKR models at line 19 and uses them elsewhere ([IBKR import/payload](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/PythonDataService/app/engine/live/account_clerk_journal_models.py#L17-L22), [ack field](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/PythonDataService/app/engine/live/account_clerk_journal_models.py#L214-L226)). |
| `engine/live/account_clerk_journal.py` | ibkr-era Account Clerk | **Loaded; not executed by an active-SQLite product request** | The mixed transaction-projection module imports its filename/model, and FastAPI imports that projection at startup. | None used by SQLite | Its IBKR journal-tail function is separate from the Alpaca JSONL-tail function ([two readers](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/PythonDataService/app/services/clerk_transaction_projection.py#L374-L475)). |
| `engine/live/account_clerk.py`, `account_clerk_lease.py`, `account_clerk_operations.py`, `account_clerk_reconciler.py` | ibkr | **Facade/lease/operations loaded at startup; reconciler is legacy-call-only; no canonical Alpaca request executes them** | `routers/broker.py` imports `manual_order_submission`, which imports `account_clerk_rpc`, which imports the old Clerk facade; `main.py` also imports old account-parity services. | None | These remain IBKR Account Clerk writer/recovery machinery. A Python import path is not evidence that an Alpaca Clerk request calls their methods. |
| `engine/live/account_clerk_rpc.py`, `account_clerk_rpc_protocol.py`, `account_clerk_cursor.py` | ibkr | **Loaded at process startup; no canonical Alpaca request executes them** | Same startup import chain through the IBKR manual-order service ([chain](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/PythonDataService/app/services/manual_order_submission.py#L9-L28), [RPC imports](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/PythonDataService/app/engine/live/account_clerk_rpc.py#L15-L42)). | None | Alpaca orders use the in-process Alpaca Clerk/facade, not the IBKR Unix-socket RPC. |
| `engine/live/account_clerk_supervisor.py`, `account_clerk_emergency_sequence.py` | ibkr | **No canonical Alpaca request path** | IBKR Clerk process supervision/emergency operation only. | None | No import from the Alpaca Clerk or canonical Alpaca routers/services was found. |
| `routers/clerk_transactions.py` → `services/clerk_transaction_projection.py` | shared route, mixed legacy projection module | **Yes — executed; old modules loaded, not executed under SQLite** | The Alpaca desk transaction-history component calls `/api/accounts/{account}/transactions` ([UI store](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/Frontend/src/app/components/broker/account-desk/account-desk-transaction-history-store.service.ts#L92-L116), [client](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/Frontend/src/app/services/broker.service.ts#L279-L298)). | `ClerkTransactionProjectionUnavailable` is a shared service exception, not an old Clerk domain model | The route tries SQLite first and returns immediately when active; only a non-SQLite account uses the legacy projection-store fallback ([branch](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/PythonDataService/app/routers/clerk_transactions.py#L78-L125)). |
| `marketdata/ibkr_feed.py`, `services/live_bar_aggregator.py` | shared boundary implemented by ibkr | **Yes — executed** | Alpaca bot task registry resolves the IBKR feed; panel LIVE chart calls the live aggregator ([startup wiring](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/PythonDataService/app/main.py#L395-L402), [bot registry](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/PythonDataService/app/main.py#L445-L459), [chart call](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/PythonDataService/app/services/broker_v2_panel/panel_chart_data_source.py#L54-L89)). | Broker-neutral `MarketDataFeed`/bar contracts | This is sanctioned IBKR **market-data execution**, not old Account Clerk or order execution. ADR 0032 says orders always use the bot's own broker Clerk ([decision](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/docs/architecture/adrs/0032-broker-contract-v2-and-verbatim-capture.md#L115-L141)). |

## The nine direct-IBKR-import candidates

All nine modules are imported into the process when `app.main` starts; the
eight modules that own routers are registered, while `broker_dependencies.py`
is a transitively imported helper
([imports](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/PythonDataService/app/main.py#L23-L81),
[mounts](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/PythonDataService/app/main.py#L687-L763)).
That makes their module-scope imports process-reachable, but it does not make
their endpoints reachable from the current Alpaca UI.

| Candidate | Current Alpaca UI request reachability | Evidence/reason |
|---|---|---|
| `account_reconciliation.py` | No caller found | Its IBKR account-reconciliation routes are mounted, but the canonical Alpaca routes and components call Alpaca Clerk/SQLite endpoints instead. |
| `bot_events.py` | No caller found | The service method remains in `LiveRunsService`, but no canonical Alpaca component invokes it. |
| `broker.py` | **Yes, executed** | Global `BrokerHealthService` polls `/api/broker/health` on all routes. |
| `broker_account_truth.py` | No caller found | Generated types and `BrokerService` methods exist; no canonical Alpaca desk/panel call was found. |
| `broker_activity.py` | No caller found | Its `/api/live-instances/...` IBKR activity surface is not called by the Alpaca desk/panel. |
| `broker_capability.py` | No caller found | Mounted IBKR capability endpoint; no canonical Alpaca caller. |
| `broker_dependencies.py` | **Yes, executed transitively** | `broker_health()` calls `is_broker_disabled()`; this module has no router of its own. |
| `live_instances.py` | No caller found | `LiveRunsService` contains methods, but the production-only banner injection does not call them; the retired `/broker/*` pages that used this family now redirect. |
| `live_runs.py` | No caller found | Same distinction: available service methods and mounted endpoints are not a canonical UI call chain. |

The supported Angular routes reinforce the boundary: all former `/broker/*`
paths are redirects, while `/brokers/alpaca` loads the Alpaca desk
([route definitions](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/Frontend/src/app/app.routes.ts#L13-L36),
[Alpaca route](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/Frontend/src/app/app.routes.ts#L269-L285)).

## Shared symbols and why they are shared

1. **`AccountClerkPositionEvidence`.** A frozen, validated tuple element for one
   non-zero broker position: symbol, signed quantity, and evidence observation
   time. Alpaca uses it to preserve the fresh position facts supporting an
   operator-confirmed accounting cutover.
2. **`AccountClerkBrokerEvidenceBaseline`.** A frozen account/time/positions
   envelope. Alpaca stores it only on `BROKER_EVIDENCE_BASELINE` journal rows so
   IBKR and Alpaca have the same namespace-free snapshot meaning
   ([Alpaca field](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/PythonDataService/app/broker/alpaca/clerk/models.py#L258-L271)).

No other direct `engine/live/account_clerk*` import exists under
`broker/alpaca/clerk/`, including `sqlite/`. The sharing boundary is nevertheless
not clean: importing either intended model executes the entire
`account_clerk_journal_models.py` module, whose other schemas carry concrete
IBKR acknowledgements/events. The Alpaca `models.py` header says it is not
coupled to the IBKR Clerk journal, while its line 22 import shows this narrow but
real exception ([header and import](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/PythonDataService/app/broker/alpaca/clerk/models.py#L1-L23)).

## Live execution flags

- **Real-money Alpaca:** no path. `AlpacaSettings` rejects every mode except
  `paper` ([validator](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/PythonDataService/app/broker/alpaca/config.py#L54-L68)); the deployment view marks
  Live as planned and not connected to admission/execution
  ([mode contract](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/PythonDataService/app/services/broker_v2_panel/paper_deploy_service.py#L342-L353)).
- **Active SQLite Alpaca paper account:** Alpaca Clerk/SQLite code executes;
  old Account Clerk modules and IBKR payload models are import-loaded, but no old
  Account Clerk function is executed by the product request. A killed SQLite
  projection returns unavailable without consulting the legacy store
  ([adversarial test](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/PythonDataService/tests/broker/alpaca/clerk/test_authority_isolation.py#L106-L166)).
- **Unactivated Alpaca paper account:** the legacy Alpaca JSONL Clerk executes.
  Its inventory-baseline recovery constructs the two shared old-lineage models;
  it still does not invoke IBKR Account Clerk RPC/lease/writer behavior.
- **Every canonical Alpaca UI route:** the global IBKR health poll executes
  `routers/broker.py` and `broker_dependencies.py` every five seconds.
- **Alpaca on-duty bot / LIVE chart:** IBKR bar/feed code executes by accepted
  design. It is read-only market data; order submission remains Alpaca Clerk
  custody.

## Adversarial refutation log

The following plausible candidates were investigated and rejected:

1. **“The Alpaca Clerk executes the old 10k-line Account Clerk.” — Refuted.**
   Alpaca directly imports only two classes from one old-lineage model module.
   No import from Alpaca Clerk to the old facade, lease, operations, reconciler,
   RPC, cursor, supervisor, or emergency sequence exists. Startup imports make
   much of that graph resident, but no canonical Alpaca call invokes it.
2. **“The shared baseline is type-only.” — Refuted.** The legacy Alpaca recovery
   constructs both classes and serializes the resulting baseline into a durable
   journal row. This candidate died in the opposite direction: the coupling is
   behavioral on that authority.
3. **“An active SQLite read silently falls back to JSONL/Postgres.” — Refuted.**
   The transaction route returns a non-`None` SQLite page before constructing
   the legacy store, and its failure test poisons the legacy store and proves it
   receives zero calls. Authority startup likewise constructs only legacy when
   no activation exists and constructs only SQLite for a valid activation
   ([selection tests](https://github.com/tim1016/learn-ai/blob/e7325d2ff0a122ffde3418cac94aa2f872f10ffa/PythonDataService/tests/broker/alpaca/clerk/test_active_authority.py#L133-L220)).
4. **“All nine IBKR-importing routers are reachable because FastAPI mounts
   them.” — Refuted.** Mounting proves direct-HTTP availability, not a current UI
   caller. Reverse searches from their URL families and forward traces from the
   Alpaca desk/panel found only the global `/api/broker/health` chain (plus its
   helper) among the nine.
5. **“`LiveRunsService` injection in the global broker banner makes
   `live_instances`/`live_runs` reachable.” — Refuted.** The banner injects the
   service for an optional active-bot notice action; production code has no
   caller that populates that notice. Test-only `setNotice` calls and service
   method definitions do not form a runtime request chain.
6. **“Generated API types prove a UI request.” — Refuted.** Generated endpoint
   shapes are compile-time contracts. Only method calls from rendered canonical
   components counted as UI reachability.
7. **“The `/broker/*` compatibility routes keep the IBKR UI live.” — Refuted.**
   Every listed route redirects to `/brokers/alpaca`; none attaches the retired
   component/provider surface.
8. **“Alpaca is fully independent of IBKR at runtime.” — Refuted.** The global
   health poll and the sanctioned signal-feed/LIVE-chart bridge execute IBKR
   code. Neither is the Alpaca order-effect boundary.
9. **“Importing the baseline classes performs IBKR broker I/O.” — Refuted.** It
   loads and defines IBKR-typed Pydantic schemas, but neither evidence class
   calls an IBKR client. Broker I/O occurs only when a separate request path
   explicitly calls a broker/read/feed operation.

## Confidence and limits

**High confidence** in the two shared symbols, authority split, global health
path, market-data bridge, and negative result for old Account Clerk function
execution under active SQLite: these have direct imports/call sites and focused
no-fallback tests. **Moderate-high confidence** in the seven negative UI-router
results: the Angular production tree and canonical route components were
searched in both directions, but a direct caller outside the repository (or a
human calling the mounted endpoint manually) cannot be excluded and is not a UI
path. Import-time reachability was established statically; this audit does not
claim which Python modules an optimizer or alternate deployment entry point
might omit. No incidental timestamp defect was identified or pursued.
