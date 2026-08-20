# IBKR integration authority

> **Current-state authority.** Last reviewed 2026-08-19 for issue #1583.
> Historical implementation detail is available in Git history; it is not an
> active product contract.

## Product boundary

IBKR is a read/evidence integration. It can observe connection capability,
accounts, positions, orders, executions, history, P&L, quotes, option data, and
bars. It cannot submit, cancel, flatten, recover, or otherwise actuate an IBKR
order.

Alpaca Broker V2 is the sole broker-control product. The deprecated `/broker/*`
URLs are redirect-only compatibility aliases and must not regain IBKR controls.

Issue #1583 retired the last IBKR order-actuation closure:

- `POST /api/broker/orders` and `DELETE /api/broker/orders/{order_id}`;
- `place_paper_order`, `cancel_paper_order`, manual submission, cancel-decision,
  and account-owned mutation helpers;
- executable Account Clerk/AccountOwner broker-write lanes and their restore,
  cure, recovery, exact-cancel, pending-cancel, and flatten operations;
- `LiveEngine`, `LiveContext`, `LivePortfolio`, the real broker adapter,
  native-time pending-order queue, and deterministic offline replay;
- the matching Angular controls, clients, handwritten types, generated contract
  entries, fixtures, tests, and current-state documentation.

There is no replacement adapter, compatibility mutation, hidden host command, or
unregistered runtime path.

## Preserved broker surface

All preserved order-related operations are non-transmitting:

| Capability | Current authority |
|---|---|
| Connection and diagnostics | `/api/broker/health`, `/data-plane/health`, `/diagnose`, plus explicit connect/disconnect/reconnect transport controls |
| Session capability | `/api/broker/capability` and `/capability/probe`; the probe may call IBKR `whatIfOrderAsync` with `whatIf=True` but never `placeOrder` |
| Account state | `/api/broker/account`, `/positions`, and `/account-truth` |
| Order evidence | `/api/broker/orders/open`, `/completed`, and `/stream` |
| Order preview | `POST /api/broker/orders/what-if`; constructs a non-transmitting IBKR what-if request only |
| API evidence | `/api/broker/ibkr/evidence` and `/ibkr/evidence/stream`, including historical callback names and error evidence |
| Market data | symbol/contract discovery, option chain/surface streams, P&L streams, and 5-second/1-minute bar snapshots |
| Session history | session-mirror snapshots, events, streams, and history; local purge operations affect evidence storage only, never broker orders |
| Account history | reconciliation receipt, account events, and transaction/history projections over durable evidence |
| Broker activity | read-only REST/SSE projection of already captured historical broker activity |

The generated OpenAPI and TypeScript contracts are authoritative for exact
request/response shapes. The structural retirement test pins the absence of
mutation methods and the presence of the read set above.

## Broker-module boundary

`PythonDataService/app/broker/ibkr/` is the only integration layer allowed to
import `ib_async`. Its order-facing modules now have narrow roles:

- `orders.py` lists open orders, streams order events, and contains builders
  shared by non-transmitting what-if previews;
- `order_history.py` reads recent completed orders;
- `order_previews.py` performs the what-if request;
- `order_projection.py` and `order_evidence.py` normalize broker callbacks into
  read models;
- `order_error_stream.py` exposes broker error evidence;
- `account_truth.py` composes account, position, open/completed order, execution,
  and durable attribution evidence.

No module defines or imports `place_paper_order` or `cancel_paper_order`.

Some names that look write-oriented remain intentionally because they are data
compatibility, not executable capability:

- `IbkrOrderSpec` is the validated input to `/orders/what-if`;
- `IbkrOrderAck` deserializes historical Account Clerk journal rows;
- `IbkrApiCallbackName.placeOrder` and `.cancelOrder` classify historical IBKR
  API evidence and errors.

Removing those schemas or literals would corrupt retained evidence without
removing any broker side effect.

## Account and host boundary

The former executable IBKR Account Clerk is absent. Durable journal/value models
remain readable because account history, contamination evidence, and the Alpaca
SQLite compatibility seam consume them. These models cannot contact IBKR.

The host bridge exposes authenticated health, Gateway socket evidence, and its
own capability-lease renewal. It cannot start an account worker or accept an
account-scoped broker command. The data-plane account cockpit, safety snapshot,
triage, reconciliation, events, and transaction endpoints are projections over
durable/read evidence. `reconcile-now` may refresh and persist evidence; it does
not place, cancel, or flatten an order.

## Safety and boundary invariants

- Every wire/storage timestamp remains `int64` milliseconds UTC.
- The paper-account sentinel and port/mode consistency remain active for broker
  connection and what-if qualification.
- What-if is opt-in and non-transmitting. It must never be reinterpreted as
  submit authorization.
- Read-only evidence may report historical submission/cancellation callbacks;
  observing or deserializing such evidence does not confer mutation authority.
- Generic Alpaca Broker V2 order routes are a separate broker authority and must
  not dispatch to IBKR.
- New replay, bot-control, or manual-order work must target the canonical Alpaca
  product rather than rebuilding an IBKR compatibility surface.

## Verification

`PythonDataService/tests/contracts/test_ibkr_order_actuation_retirement.py`
proves:

1. retired Python modules and registered API methods are absent;
2. production Python contains no place/cancel primitive reference;
3. the executable Account Clerk and host mutation routes are absent;
4. Frontend production sources contain no orphaned order/recovery clients;
5. generated OpenAPI/TypeScript contracts omit every retired operation and
   offline replay route; and
6. the preserved account, position, order/history, what-if, evidence, capability,
   bars, session-history, reconciliation, event, and transaction reads remain.

The paired authority registries are
`docs/architecture/engine-authority-map.md` and
`docs/math-sources-of-truth.md`. The historical rationale for the original
integration is marked retired in `docs/architecture/ibkr-integration-tdd.md`.
