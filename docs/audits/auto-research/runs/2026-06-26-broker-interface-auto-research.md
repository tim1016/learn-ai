# Broker interface auto-research — 2026-06-26

**Mode:** ad-hoc broker/interface auto-research after PR #690 merge.  
**Started:** 2026-06-25T23:20:35-0500 local / 2026-06-26 UTC.  
**Git:** `master` at `2f86597d` (`tim1016/learn-ai#690`, merged 2026-06-26T04:18:18Z).  
**Scope:** Interactive Brokers request/response coverage, Bot Cockpit Activity projection, deploy/configuration docs, and post-PR #690 trader/operator visibility.  
**Constraints:** read-only outside `docs/audits/auto-research/`; no production code, tests, fixtures, branches, commits, or dependency changes.

## Executive verdict

The PR moved the system in the right direction: deploy now derives broker account identity from the connected broker session before forwarding to the host daemon, Activity repair uses a derived projection/cache rather than mutating the canonical broker-activity WAL, and the Activity table now has backend-authored trader-language rows for fills, order intents, terminal states, closed-trade summaries, and generic IBKR evidence.

But the broker interface is not yet "clear-cut full interface" quality. The main gap is provenance fidelity: IBKR evidence is captured and projected, but row-level matching is too broad and the UI does not let the operator drill into the evidence behind normalized Activity event rows.

## Findings opened

- F-0037 — P1 — wire — Activity projection attaches session-wide IBKR evidence to individual fill/order rows by request type only.
- F-0038 — P2 — frontend-consumption — Activity event rows show evidence counts but do not expose row-level evidence details.
- F-0039 — P2 — documentation — ADR/runbook language overstates the trader-language completion of the Configuration tab.

## Request/Response Coverage Notes

| IBKR interaction | Current capture / projection | Display state | Gap |
|---|---|---|---|
| `placeOrder` | Raw API evidence recorder + broker activity WAL / projection refs | Activity event evidence count; legacy broker row drawer for old row type | Evidence refs are session-wide by request type for event rows, not row-matched |
| `reqExecutionsAsync` / execution callbacks | Raw API evidence + reconstructed fill rows | Broker fills / repaired trade evidence rows | Drill-down absent for new event rows |
| `reqAllOpenOrders` / order status | Raw API evidence + order intent / terminal rows | Activity table summary rows | Evidence attribution is broad; raw details not row-inspectable |
| `reqPositionsAsync` | Raw API evidence + folded broker evidence row; fleet/account summary uses net positions | Activity generic evidence row / account notices | Good high-level visibility, but details require separate diagnostics panel |
| `accountSummaryAsync`, `reqPnL`, `reqPnLSingle` | Raw API evidence narratives exist | Generic Activity evidence rows when present; PnL streams elsewhere | Not yet presented as a unified IBKR interface matrix |
| `reqRealTimeBars`, `reqMktData`, contract qualification, option metadata/search | Narrative registry entries exist | Generic evidence rows when captured | Needs explicit docs and row drill-down to prove full round-trip |

## Documentation Hardening Notes

The docs should be split into two layers before trader-language manualization:

1. **Implementation-accurate broker interface map:** every IBKR request/callback/data artifact we send/receive, where it is captured, where it is normalized, whether it persists, which endpoint exposes it, and which UI surface displays it.
2. **Trader manual:** only after the map is correct, rewrite the operator runbook in account/portfolio/positions/orders/fills/P&L language.

ADR 0016 is a good target shape, but F-0039 shows it is ahead of the current Configuration tab implementation. Treat it as desired architecture unless the docs are amended to say which slices are still partial.

## Suggested Next Research Slice

Run a second pass focused on actual broker adapter coverage:

- Build an inventory from `PythonDataService/app/broker/ibkr/*` and `PythonDataService/app/routers/broker.py` of every IBKR call and callback we use.
- For each call, trace: request model → IBKR call → raw evidence event → normalized schema → persistence artifact → API/Activity projection → UI display.
- Classify each as `displayed`, `captured-not-displayed`, `displayed-without-raw-detail`, `not-captured`, or `not-applicable`.
- Re-check the previously found disconnect/stale-stream cluster against the merged master because the current broker docs should not imply liveness is fully proven until those findings are resolved or superseded.

## Stop reason

Initial tick complete with concrete findings. Broader adapter-by-adapter coverage matrix remains open for the next auto-research pass.

## Follow-up completion

Follow-up branch `codex/fix-ibkr-activity-evidence` fixed F-0037 through F-0039 and added the requested adapter matrix:

- `docs/audits/auto-research/runs/2026-06-26-ibkr-adapter-matrix.md`
- `PythonDataService/app/services/activity_evidence_matching.py`
- `Frontend/src/app/components/broker/cockpit-v2/reused/broker-activity-table/*`
- `Frontend/src/app/components/broker/cockpit-v2/tabs/configuration-tab.component.*`

The matrix now traces every typed IBKR request/callback currently in `IbkrApiRequestName` and `IbkrApiCallbackName` through raw evidence, normalized schema, persistence, API, and UI display. It also records the remaining full-interface gaps for `reqCurrentTimeAsync`, `errorEvent`, and durable raw-evidence retention.
