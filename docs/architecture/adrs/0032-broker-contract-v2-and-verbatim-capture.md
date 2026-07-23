# ADR 0032: Broker Contract v2 and Verbatim Capture

- **Date:** 2026-07-22
- **Status:** Accepted
- **Context:** Issue #1169 (Alpaca broker integration, phase 1). Design spec:
  `docs/superpowers/specs/2026-07-21-alpaca-broker-integration-design.md`.

## Decision

Add Alpaca as the platform's second brokerage (paper only) as **Broker System
v2**: a broker-neutral contract with a vendor layer behind it, contract-native
from day one, fully independent of the existing IBKR (v1) stack. IBKR is treated
as the **legacy exception**, not the template; v1 code is untouched in phase 1.

### Four layers

```text
app/broker/
├── contract/   # Layer 3 — broker-neutral: models, capabilities, errors, ports, registry
├── capture/    # Layer 2 — broker-neutral verbatim JSONL journal
└── alpaca/     # Layer 1 + adapter — speaks pure Alpaca
```

1. **Contract models are the only broker types that cross the router boundary.**
   No alpaca-py type escapes `app/broker/alpaca/`. Every contract timestamp is
   `int64` ms UTC (`temporal-rigor.md`); the adapter is the single ingestion
   boundary where vendor RFC-3339 strings convert exactly once.
2. **Capabilities over identity.** Callers gate on `BrokerCapabilities` data
   (`bars_may_gap`, `max_stream_symbols`, `data_feed`, order types), never on an
   `if broker == "alpaca"` branch. Honest vendor differences are declared as
   data.
3. **Files-canonical capture (decision D3).** Every response — success *or*
   error — is journaled verbatim to an append-only JSONL file before the SDK
   parses it. Files are canonical; any future Postgres projection is rebuildable
   from them and never authoritative. On the read path a capture failure is
   non-fatal (logged + counted); the phase-2 order path flips this to
   fail-closed (no journal → no order).
4. **Registry seam.** `broker_id → BrokerReadPort`; the router resolves the
   `{broker}` path segment, unknown → 404. Ports construct lazily so the service
   boots without credentials.

### alpaca-py in `raw_data` mode (decision D4)

We use the **official alpaca-py SDK** (over a hand-rolled httpx client) for its
maintained models, enums, request builders, auth, URL derivation, and retry —
the alternative (raw REST + hand-written models) was rejected for
model-maintenance cost. We drive it in **`raw_data=True`** mode, so the client
returns the parsed JSON and the **adapter** is the single, explicit
ingestion/temporal boundary — the SDK performs no hidden datetime parsing. This
also keeps adapter golden-fixture tests hermetic (they feed captured JSON dicts;
no SDK needed to run them).

Verbatim capture is preserved with a **`requests.Session` response hook** on the
SDK's session (the hook reads only body + URL/method/query, never auth headers).
A **schema-compatibility test** recursively diffs captured payload keys against
the alpaca-py model fields (and aliases) and fails naming any key the SDK does
not know. Adapter tests independently assert the contract mappings. Because alpaca-py drives
`requests` (which respx/pytest-httpx cannot intercept), the capture-hook tests
use the `responses` dev-dependency, the only requests-level mock that exercises
the real Session hook path.

### Safety and authority

- **Paper-only (decision D7).** `AlpacaSettings` refuses any mode other than
  `paper` (a validator raises); the base URL is derived from the mode, never
  independently configurable. Live enablement is a deliberate future change.
- **Clock/calendar is vendor evidence only.** `/v2/clock` and `/v2/calendar`
  are captured and surfaced as `BrokerClockEvidence`; the canonical calendar
  module (`temporal-rigor.md`) remains the sole authority for scheduled session
  structure. A parity diagnostic comparing the two is welcome later; an
  authority change is not.

## Consequences

- New endpoints live under `/api/brokers/{broker}/...`, transport-only in
  `app/routers/brokers.py` (router-freeze discipline). They update the committed
  OpenAPI snapshot (`contracts/openapi/...`) and the generated `broker.types.ts`
  (ADR 0031's contract-generation gate).
- A new runtime dependency, `alpaca-py`, is pinned in `requirements-light.txt`
  (pinned tight because the raw-payload field set is validated against that
  exact version by the schema-drift test).
- Live-bar streaming, order submission, the `trade_updates` consumer, an IBKR
  contract adapter, and any Postgres projection are explicitly out of phase 1
  (design spec §11–§12).

## V1 Goodness Inventory (expanded)

Every good behavior of the IBKR (v1) stack lands in exactly one column, with a
one-line rationale. V2 is greenfield, audited against v1 — not a copy-prune fork
(decision D6).

| # | V1 good thing (IBKR stack) | V2 treatment | Rationale |
|---|---|---|---|
| 1 | 3-layer config safety (mode + port + DU-prefix validators) | **Ported** | `AlpacaSettings`: mode validator refuses non-paper + base URL derived from mode; Alpaca paper isolation replaces the port/account-prefix layers (no sockets, no DU prefix). |
| 2 | API evidence stream (volatile in-process deque) | **Improved** | Durable append-only JSONL capture journal (§6) replaces the volatile deque; survives restarts and is the fixture-regeneration source. |
| 3 | All-responses-including-errors evidence | **Ported** | The hook journals error responses too (a 403 is evidence); verified by tests. |
| 4 | Secret hygiene in evidence | **Ported** | The hook never forwards auth headers; the journal redacts secret-like params as defence in depth. |
| 5 | Stream duplicate policy (`strict` vs `live_idempotent`, `bars.py`) | **Deferred** | Belongs to the phase-2/3 Alpaca stream consumers; no stream exists in phase 1. |
| 6 | Halt-on-connection-death → recovery flatten | **Deferred** | Contract behavior for `BrokerBarStreamPort` (phase 3); the phase-1 read paths have no live loop. |
| 7 | Structured operator diagnostics / health surfaces | **Ported (pattern)** | Capture counters (`records_written`, `failure_count`) are the phase-1 seed; a broker health endpoint follows in phase 2+. |
| 8 | Typed error taxonomy at the seam | **Improved** | Broker-neutral `BrokerError` family with honest `http_status` mapping, shared across all brokers, replaces IBKR-specific exception handling. |
| 9 | int64-ms-UTC normalization at the model seam | **Ported** | The adapter converts every vendor timestamp to `int64` ms UTC exactly once, at ingestion. |
| 10 | Parquet persistence writers (ticks/account/PnL) | **Deferred** | The JSONL journal covers phase-1 audit; revisit for stream data in phase 3. |
| 11 | Reconnect monitor, session mirror, client-ID management, gateway babysitting | **N/A** | Exists only because of IBKR's stateful socket model; Alpaca is stateless HTTPS + websocket. |
| 12 | Contract qualification machinery | **N/A** | Alpaca uses plain symbols / asset IDs; no contract search is needed. |
