# Alpaca Broker Integration — Design (Broker System v2)

**Date:** 2026-07-21
**Status:** Approved design, pre-implementation
**Decision owners:** Inkant (product/architecture decisions recorded below)

## 1. Summary

Add Alpaca as the platform's second brokerage, paper trading only, positioned as
**v2 of the broker system**: its own backend route family, its own frontend
surface, contract-native from day one, and fully independent of the existing
IBKR (v1) stack. Phase 1 delivers a typed Alpaca client with verbatim raw-payload
capture and a read-only UI. No IBKR code is touched.

## 2. Decisions locked in this design

| # | Decision | Choice |
|---|---|---|
| D1 | Phase-1 scope | Typed client + raw capture + **read-only** UI (account, positions, orders). No order submission. |
| D2 | Architecture | Broker-first 4-layer: vendor client / raw capture / broker contract / adapter. IBKR is treated as the legacy exception, not the template. |
| D3 | Raw capture medium | **Files canonical** (append-only JSONL journal). Postgres projection only if/when SQL querying becomes a real need; the projection is rebuildable from files, never authoritative. |
| D4 | Wire client | An owned `requests.Session` transport over Alpaca's documented REST API, with a public response hook for verbatim capture before parsing; a schema-drift test guards field coverage. |
| D5 | Data authority | Polygon remains the sole authority for signals, research, and backtesting. During live trading, each broker's own stream feeds bars (mirrors current IBKR behavior). Live-bar streaming is a formal broker capability, designed now, built in phase 3. |
| D6 | V2 build strategy | Greenfield, **not** a copy-prune fork of the IBKR stack. V1's good things are carried via the V1 Goodness Inventory (§10): each item ported, improved, or marked N/A. |
| D7 | Safety posture | Phase 1 hard-refuses `ALPACA_MODE` other than `paper` (validator raises). Live enablement is a deliberate future change. |

## 3. Background findings that shaped the design

- The current broker layer has **no abstraction**: everything is IBKR-concrete
  under `app/broker/ibkr/` (34 files); IBKR types are imported by 9 routers and
  `app/engine/live/account_clerk.py`. There is no multi-broker pattern to slot
  into — this design defines the seam.
- A large fraction of the IBKR package manages IBKR's **stateful socket model**
  (reconnect monitors, session mirror, client-ID juggling, gateway health,
  5s→1min bar aggregation). Alpaca is stateless HTTPS + websocket; almost none
  of that machinery applies.
- **Polygon is never used in the live loop today.** Live bars come from IBKR
  `reqRealTimeBars` (5-second bars folded to closed 1-minute bars in
  `app/broker/ibkr/bars.py`); warmup rehydrates persisted indicator state;
  cold-start backfill uses IBKR historical bars. Trading halts when the IBKR
  *connection* dies (`IBKRBarStreamError` → recovery flatten), not when bars
  merely pause. The Alpaca design preserves exactly this split and this halt
  semantic.
- **Alpaca market data (verified 2026-07):** Basic plan (free, included with
  every paper account) = real-time IEX-only feed via websocket including a
  closed 1-minute bars channel; 30 symbol subscriptions; 1 concurrent
  connection; 200 REST calls/min; recent-15-min history blocked for `feed=sip`
  but open for `feed=iex`. Algo Trader Plus ($99/mo) = full SIP feed; upgrade is
  a config change (`feed=iex` → `feed=sip`), zero architecture change. Caveat:
  IEX is ~2.5–3% of consolidated volume, so **bar gaps on illiquid symbols are
  normal feed behavior, not an outage** — liveness logic must treat only
  stream/connection death as fatal (same as IBKR today). The `trade_updates`
  stream is part of the free per-account Trading API, independent of market-data
  plans.

## 4. Architecture — four layers

```
app/broker/
├── contract/                    # Layer 3 — broker-neutral (the v2 center of gravity)
│   ├── models.py                #   BrokerAccountSnapshot, BrokerPosition, BrokerOrder,
│   │                            #   BrokerOrderEvent, BrokerActivity, BrokerClockEvidence
│   ├── capabilities.py          #   BrokerCapabilities descriptor
│   ├── errors.py                #   BrokerAuthError, BrokerRateLimited, BrokerRequestInvalid,
│   │                            #   BrokerOrderRejected, BrokerUnavailable
│   ├── ports.py                 #   BrokerReadPort (phase 1); BrokerTradePort and
│   │                            #   BrokerBarStreamPort declared in later phases
│   └── registry.py              #   broker_id → port implementation; phase 1 registers "alpaca"
├── capture/                     # Layer 2 — broker-neutral raw capture
│   └── journal.py               #   CaptureJournal (append-only JSONL, §6)
└── alpaca/                      # Layer 1 + adapter — speaks pure Alpaca
    ├── config.py                #   AlpacaSettings (env_prefix="ALPACA_")
    ├── client.py                #   owns the documented REST transport; sync calls run via
    │                            #   anyio.to_thread (repo mandates async I/O at the service layer)
    ├── capture_hook.py          #   requests.Session response hook → CaptureJournal
    ├── adapter.py               #   from_alpaca_account(...) etc. → contract models
    └── errors.py                #   HTTP status / transport error → contract errors
```

Rules:

- **Contract models are the only broker types that cross the router boundary.**
  No alpaca-py type escapes `app/broker/alpaca/`.
- **Callers gate on capabilities, never on broker identity.** `BrokerCapabilities`
  declares honest differences as data (e.g. Alpaca-IEX: `bars_may_gap=True`,
  `max_stream_symbols=30`; order-type support; fractional; extended hours).
- All contract-model timestamps are `int64 ms UTC` per
  `.claude/rules/temporal-rigor.md`. The Alpaca adapter is the ingestion
  boundary: RFC-3339 vendor strings convert exactly once, there.
- Alpaca `/v2/clock` and `/v2/calendar` are captured and exposed strictly as
  **vendor evidence** (`BrokerClockEvidence`). The canonical calendar module
  remains the sole session-structure authority. A parity diagnostic comparing
  the two is welcome later; an authority change is not.

### Ports (phase 1)

`BrokerReadPort` (Protocol): `get_account()`, `list_positions()`,
`list_orders(status, limit, after_ms)`, `list_assets(status)`,
`get_clock_evidence()`. All async; implementations wrap the sync transport in
a threadpool. Account activities are not included in phase 1: alpaca-py's
supported activities surface is `BrokerClient`, whose Broker API is for a
different account model. Phase 2 may add a dedicated documented Trading API
activities transport if it is still required.

### Router

New thin router `app/routers/brokers.py` (transport only, per the router-freeze
discipline): `GET /api/brokers/{broker}/account | positions | orders | assets |
clock`. The registry resolves `{broker}`; unknown broker →
404 with a typed detail. Phase 1 registers only `alpaca`. The v1 route family
(`/api/broker/...`) is untouched. New endpoints update the committed OpenAPI
snapshot under `contracts/` (CI contract gate).

## 5. Data flow (read path)

```
Frontend → GET /api/brokers/alpaca/positions
  → router → registry → AlpacaBroker adapter
    → client.py (anyio.to_thread) → owned requests.Session → HTTPS
      ← public response hook appends VERBATIM response bytes before parsing
      ← client maps the payload into the adapter's vendor DTO
    ← adapter maps to BrokerPosition (strings → int64 ms UTC here)
  ← JSON response (snake_case contract model)
```

The journal's `raw_body` is an opaque audit blob: it retains Alpaca's exact
payload bytes, including vendor timestamp strings, and is never exposed as a
structured storage or wire field. Every temporal value outside that blob,
including `captured_at_ms` and every contract-model field, is `int64 ms UTC`.

## 6. Capture journal

- **Location:** `<BROKER_CAPTURE_DIR>/<broker>/<endpoint-family>/<YYYY-MM-DD>.jsonl`
  (endpoint families: `account`, `positions`, `orders`, `assets`, `clock`),
  rotation by UTC day. `BROKER_CAPTURE_DIR` is a capture-layer env
  var (not `ALPACA_`-prefixed — it is broker-neutral), default
  `PythonDataService/var/broker_captures/` (git-ignored).
- **Line format:**
  `{"broker","endpoint","method","params","status","captured_at_ms","raw_body"}`.
  `captured_at_ms` and any structured temporal request parameter are `int64 ms
  UTC`; `raw_body` is the opaque verbatim response text audit blob. A non-UTF-8
  body is stored base64 with `"body_encoding":"base64"`.
- **All responses are captured, including errors.** A 403 from Alpaca is
  evidence too.
- **Secrets never enter the journal.** The hook redacts auth headers and never
  records key material; `params` contains query/body parameters only.
- **Failure policy (phase 1, reads):** capture is a prerequisite for a response.
  If durable append fails, the client returns a typed unavailable error and the
  router returns 503; it never serves an uncaptured account, position, or order
  read. The failure logs ERROR and increments an observable counter. **Phase 2
  applies the same rule to the order path: no journal, no order** (the clerk's
  inbox discipline).
- Single writer per process; append + flush per line (fsync deferred to the
  phase-2 order path).
- Journals are the regeneration source for golden fixtures and for any future
  Postgres projection.

## 7. Config, safety, dependencies

- `AlpacaSettings` (pydantic-settings, `env_prefix="ALPACA_"`, `.env` only —
  never committed): `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY`,
  `ALPACA_MODE`. (`BROKER_CAPTURE_DIR` lives with the capture layer, §6.)
- Safety validators (ported from v1's 3-layer pattern): phase 1 raises unless
  `mode == "paper"`; base URL is derived from mode
  (`https://paper-api.alpaca.markets`), never independently configurable, so a
  mode/URL mismatch cannot exist.
- Dependencies: `requests` is already transitive in the current service
  environment; phase 1 declares it explicitly and pins it in
  `PythonDataService/requirements-light.txt`. An owned documented REST
  transport was chosen over `alpaca-py` because the SDK exposes no public
  request-session or response-hook injection point; accessing its private
  `_session` would make canonical capture upgrade-fragile. `alpaca-py` remains
  a candidate for its public websocket surface in phase 3. Dev-dep `responses`
  in `requirements-dev.txt` exercises the real Session hook path (see §9).

## 8. Frontend (v2 surface)

- New lazy route `/brokers/alpaca` (`loadComponent`), separate from all v1
  broker pages. Components in `Frontend/src/app/components/brokers/alpaca-desk/`:
  account card (equity, cash, buying power, status, paper badge), positions
  table, orders table. Read-only; no actions.
- Signals + `resource()`; OnPush; honest-empty states — "no positions" and
  "couldn't reach Alpaca" are distinct renders.
- `brokers.service.ts` against `/api/brokers/alpaca/...`; types generated via
  the existing OpenAPI pipeline (sibling of `broker.types.ts`).
- Timestamps render through the shared timestamp display component (`local`
  mode for instants). Code-like identifiers render through `receiptLabel`.
  AXE / WCAG AA apply.

## 9. Error handling & testing

**Error mapping** (in `alpaca/errors.py`, asserted by tests): 401/403 →
`BrokerAuthError`; 429 → `BrokerRateLimited` (carries retry-after); 422 →
`BrokerRequestInvalid`; 5xx/network → `BrokerUnavailable`. The router
translates contract errors to HTTP responses with what/why detail per the
error-authoring standard. No silent catches.

**Tests:**

- Adapter: golden captured payloads → contract models, every field asserted
  (this is where "100% payload mapping" is proven).
- **Schema-drift test:** recursively diffs captured payload key sets against
  the owned vendor DTO and adapter coverage map; fails naming unknown keys when
  Alpaca ships a field the contract does not map. This enforces the
  no-fields-dropped rule; the journal always has everything regardless.
- Capture journal: verbatim byte round-trip, UTC-day rotation, error-response
  capture, base64 fallback, secret-redaction.
- Settings: `ALPACA_MODE=live` refused; URL derivation.
- Router: `httpx.AsyncClient` + `ASGITransport` with a fake `BrokerReadPort`
  bound in the registry.
- Transport boundary: capture-hook tests use the `responses` dev-dep to
  exercise the owned public Session-hook path; client-wrapper tests mock the
  documented REST responses at that boundary.
- Frontend: Vitest + Angular Testing Library, service mocked at DI level,
  empty/error states asserted.
- First real paper-account captures are sanitized (account IDs, order IDs where
  linkable) and committed as golden fixtures under
  `PythonDataService/tests/fixtures/alpaca/`.

## 10. V1 Goodness Inventory

V2 is greenfield but audited against v1. Implementation planning expands this
table; every v1 item must land in exactly one column.

| V1 good thing (IBKR stack) | V2 treatment |
|---|---|
| 3-layer config safety (mode + port + DU-prefix validators) | **Ported**: mode validator + derived base URL + paper-key cross-checks in `AlpacaSettings` |
| API evidence stream (volatile in-process deque) | **Improved**: durable JSONL capture journal (§6) |
| Stream duplicate policy (`strict` vs `live_idempotent`, `bars.py`) | **Ported** into Alpaca stream consumers (phases 2–3) |
| Halt-on-connection-death → recovery flatten | **Ported** as contract behavior for `BrokerBarStreamPort` (phase 3) |
| Structured operator diagnostics / health surfaces | **Ported** pattern: capture counters + broker health endpoint (phase 2+) |
| Parquet persistence writers (ticks/account/PnL) | **Deferred**: journal covers audit; revisit for stream data in phase 3 |
| Reconnect monitor, session mirror, client-ID management, gateway babysitting | **N/A** — exists only because of IBKR's stateful socket model |
| Contract qualification machinery | **N/A** — Alpaca uses plain symbols/asset IDs |

## 11. Phase roadmap

| Phase | Content |
|---|---|
| **1 (this design)** | Contract + capture journal + Alpaca client/adapter + `/api/brokers/` router + read-only UI + tests + ADR (`docs/architecture/adrs/` — broker contract v2 & capture; number assigned at implementation) |
| **2** | `BrokerTradePort` (submit/cancel), `trade_updates` websocket consumer with capture, manual order UI, capture-before-submit (no journal → no order) |
| **3** | `BrokerBarStreamPort`: Alpaca IEX 1-min bars channel feeds the live loop; `feed=sip` upgrade by config; Alpaca-driven bots |
| **4** | IBKR strangler: `IbkrBrokerAdapter` implements the contract, v1 surfaces migrate to `/api/brokers/ibkr/...`, clerk generalizes off `IbkrOrderSpec` |
| any time | Rebuildable Postgres projection of capture journals, if SQL querying becomes a real need |

## 12. Out of scope (phase 1)

- Order submission, cancellation, any write path.
- Alpaca market-data endpoints and streams (phase 3; Polygon authority
  unchanged everywhere outside the live loop).
- Any change to `app/broker/ibkr/`, the 9 IBKR-coupled routers, the clerk, the
  daemon, or v1 frontend pages.
- Live-mode (real money) Alpaca access.
- Postgres anything.
