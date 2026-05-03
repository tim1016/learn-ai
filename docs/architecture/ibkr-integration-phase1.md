# IBKR integration — Phase 1: read-only option chain

**Status**: Phase 1 implemented, May 2026.
**Scope**: One symbol (SPY), one expiry at a time, streaming top-of-book + Greeks/IV from IBKR side-by-side with the engine's own calculations. No order placement.

## Goal

Connect to an IBKR paper account, stream a real-time option chain with IBKR-computed IV / Greeks, and surface the data alongside the engine's own validated math (Hull / py_vollib / QuantLib). The reconciliation between IBKR's values and ours becomes a third independent authority for the math-sources-of-truth registry.

This phase is read-only. No `placeOrder`, no `cancelOrder`, no portfolio mutation. Order placement is Phase 3 and gated behind explicit user approval.

## Design philosophy

> "When we integrate an external API, WE take whatever endpoints they expose and we use them although we might expose only a few of them in the functionality but like to have tight coupling there."

The internal `app.broker.ibkr.*` package is tightly coupled to the `ib_async` API surface — the wrapper exposes the `IB()` instance directly to in-package callers so future phases can reach for any primitive. The FastAPI router (`app.routers.broker`) is the curated boundary: only what we want the .NET / Angular layers to see crosses into HTTP.

## File map

```
PythonDataService/
├── app/
│   ├── broker/
│   │   └── ibkr/
│   │       ├── __init__.py
│   │       ├── config.py          # IbkrSettings — env vars + port/mode validator
│   │       ├── models.py          # Pydantic v2 wire models (int64 ms UTC throughout)
│   │       ├── client.py          # IbkrClient — connect, sentinel check, lifecycle
│   │       ├── contracts.py       # qualify_underlying, list_expirations, list_strikes, build_chain_contracts
│   │       ├── market_data.py     # stream_option_chain — async iterator of IbkrChainSnapshot
│   │       └── persistence.py     # NoopTickWriter / ParquetTickWriter behind IBKR_PERSIST_TICKS
│   ├── routers/
│   │   └── broker.py              # /api/broker/{health, expirations/{symbol}, option-chain/{symbol}}
│   └── main.py                    # lifespan event owns the IbkrClient
└── tests/
    └── broker/ibkr/
        ├── test_config.py
        ├── test_models.py
        ├── test_client.py
        ├── test_contracts.py
        ├── test_market_data.py
        ├── test_persistence.py
        └── test_router.py
```

## Paper-vs-live safety — three layers

The integration enforces paper-mode at three independent points so any one mistake fails closed:

1. **`IBKR_MODE` env var.** Default `paper`. Refuses to flip to `live` without an explicit set. Lives in `config.py`.
2. **Port validator.** `config.py` rejects any combination of `mode=paper` with a known-live port (4001/7496) and any `mode=live` with a known-paper port (4002/7497). A copy-pasted `IBKR_PORT=4001` cannot quietly route a paper build to a live socket.
3. **Account-ID sentinel.** `client.py::IbkrClient.connect` reads `managedAccounts()` post-connect and asserts that paper IDs begin with `DU`. Mismatch → immediate disconnect + `ConnectionRefusedDueToSentinelError`. Lifespan event re-raises this; the service refuses to start.

Future Phase 4 (live) will add a fourth: per-request `confirm_live=true` on order-placing endpoints.

## Data flow

```
                  ┌─────────────────┐
                  │  IB Gateway     │
                  │  (Windows host) │
                  └────────┬────────┘
                           │ TCP 4002 (paper)
                           ▼
        ┌─────────────────────────────────────────┐
        │ app.broker.ibkr.client.IbkrClient       │
        │  - connectAsync, sentinel check         │
        │  - exposes IB() to in-package callers   │
        └────────┬────────────────────────────────┘
                 │
        ┌────────▼────────────┐    ┌──────────────────────┐
        │ contracts.py        │    │ market_data.py       │
        │  qualify_*          │    │  stream_option_chain │
        │  list_expirations   │    │  (async iterator)    │
        │  list_strikes       │    └────────┬─────────────┘
        │  build_chain        │             │
        └─────────────────────┘             ▼
                                  ┌──────────────────────┐
                                  │ persistence.py       │
                                  │  ParquetTickWriter   │
                                  │  (behind flag)       │
                                  └──────────────────────┘
                                            │
                                            ▼
                              ┌──────────────────────────┐
                              │ app.routers.broker       │
                              │  /api/broker/health      │
                              │  /api/broker/expirations │
                              │  /api/broker/option-chain│
                              │       (Server-Sent Ev)   │
                              └────────┬─────────────────┘
                                       │ HTTP / SSE
                                       ▼
                         (.NET backend → GraphQL → Angular UI)
```

## Endpoint contract

### `GET /api/broker/health`

Always 200. The body is the `IbkrConnectionHealth` model. UI reads `mode` and `is_paper` to render the paper/live banner.

```json
{
  "mode": "paper",
  "host": "host.docker.internal",
  "port": 4002,
  "client_id": 1,
  "connected": true,
  "account_id": "DU1234567",
  "is_paper": true,
  "server_version": 178,
  "fetched_at_ms": 1761234567890
}
```

### `GET /api/broker/expirations/{symbol}`

```json
{ "symbol": "SPY", "expirations_ms": [1778976000000, 1779580800000, ...] }
```

### `GET /api/broker/option-chain/{symbol}?expiry_ms=&strike_min=&strike_max=&debounce_ms=250`

Server-Sent Events. Each `event: chain` carries an `IbkrChainSnapshot` payload:

```
event: chain
data: {"symbol":"SPY","expiry_ms":1778976000000,"underlying_price":420.42,"quotes":[...],"as_of_ms":1761234568145}

event: chain
data: ...
```

On broker error, an `event: error` is emitted, then the stream closes.

## Reconciliation play

Each `IbkrOptionQuote` carries IBKR's IV/Greeks tagged with `greeks_source` (`"model"` / `"bid"` / `"ask"` / `"last"` / `"none"`). The same `(strike, right, ts_ms)` is computed by the engine via QuantLib + py_vollib. The Angular table renders both side-by-side; persistent unexplained gaps become entries in `docs/math-sources-of-truth.md`, the same way the SPX-vs-SPY 19 bps CBOE reconciliation already lives there.

## Local run

1. Install IB Gateway, log in with **paper** credentials.
2. Subscribe to "US Equity and Options Add-On Streaming Bundle" on the **live** account ($4.50/mo + $10 prerequisite); paper inherits.
3. In Gateway: Configure → API → Settings: enable ActiveX and Socket Clients, port `4002`. If running the Python service in Podman/WSL on Windows, `IBKR_HOST` should target the Windows-side WSL bridge IP (for example `172.23.176.1`), but Gateway's trusted IPs must include the container/VM peer IP that Windows sees in `netstat` (for example `172.23.176.94`). `127.0.0.1` is only enough for native host processes.
4. From the repo:
   ```
   ./restart.sh        # rebuild Python container with ib_async
   curl http://localhost:8000/api/broker/health
   ```
5. SSE smoke:
   ```
   curl -N "http://localhost:8000/api/broker/option-chain/SPY?expiry_ms=<int64-ms>&strike_min=580&strike_max=600&debounce_ms=500"
   ```

## Phase 2 / Phase 3 / Phase 4 follow-ups

- **Phase 2 — account & P&L**: positions snapshot endpoint, P&L SSE stream, mirroring TWS "Portfolio" tab inside the app.
- **Phase 3 — paper order placement**: `placeOrder` wrapper, gated on `IBKR_MODE=paper`. Reconciliation of every fill against the engine's `FillModel` prediction.
- **Phase 4 — live transition**: per-request `confirm_live` flag, frontend banner switch, deliberate operator opt-in.
- **Phase 1.5 — persistence schema**: the `ParquetTickWriter` is intentionally minimal. A separate ticket will choose final layout (date partition vs hive-style, retention, replay tooling) and the schema-versioning convention.
- **Phase 1.5 — multi-account support**: `IbkrClient.connect` warns on >1 managed account and uses the first. Multi-account FA structures need explicit selection.

## Tests

Run from project root:

```
podman exec polygon-data-service python -m pytest /app/tests/broker -v
```

All Phase 1 tests are unit tests with `ib_async.IB` patched out — they run on hosts that don't have ib_async installed. Integration tests against a live Gateway live in `tests/integration/broker/` (not yet written) and are skipped unless `IBKR_INTEGRATION=1` is set.

## Lessons learned (debugging notes from initial bring-up)

Two non-obvious traps cost a lot of time during the first-light bring-up under Podman-on-Windows. Both have regression tests; both are documented here so the next person doesn't repeat them.

### 1. `pydantic-settings` `case_sensitive=True` silently drops uppercase env vars

**Symptom**: `podman exec polygon-data-service sh -c 'echo $IBKR_HOST'` showed the right value (`172.23.176.1`), but the Python app's `IbkrSettings.host` resolved to `"auto"`. The connect log showed `IBKR_HOST=auto resolved to default gateway 10.89.0.1`, not the value compose was injecting.

**Root cause**: `IbkrSettings` was declared with `case_sensitive=True`. With that setting, `pydantic-settings` matches env vars against the **field name's literal case** plus the prefix — so it was looking for `IBKR_host` (lowercase `host`), not `IBKR_HOST`. Compose was setting `IBKR_HOST` (uppercase, the only standard 12-factor convention), so pydantic-settings ignored it and used the default. Critically, **no warning is emitted** — the setting is just silently the default.

**Fix**: `case_sensitive=False` in `IbkrSettings.model_config` (config.py). Now `IBKR_HOST`, `IBKR_host`, `ibkr_host` all match. Regression test: `tests/broker/ibkr/test_config.py::test_uppercase_ibkr_env_vars_are_honored`.

**Why we missed it**: the bug only fires when the field name's case differs from the env var's case. Our fields are `host`, `mode`, `port` (all lowercase by Python convention); compose sets `IBKR_HOST`, `IBKR_MODE`, `IBKR_PORT` (all uppercase by Unix convention). Both conventions are correct in isolation; the trap is that the *default* `case_sensitive=True` requires them to agree.

### 2. Gateway "Trusted IPs" must include the WSL VM's peer IP, not just the destination IP

**Symptom**: After fixing #1, the container was reaching `172.23.176.1:4002` (the Windows-side WSL bridge IP), `connect_ex` returned `0` (the OS accepted the SYN), but `ib_async.connectAsync` still saw `ConnectionRefusedError(111)` once the Gateway-level handshake started.

**Root cause**: IB Gateway's "Trusted IP Addresses" allowlist filters by the *source* of the inbound TCP connection — i.e. the IP **Windows sees the connection coming from**, not the IP the container *sent to*. From the Windows host's perspective, traffic from a Podman container in WSL2 originates from the WSL VM's peer IP (`172.23.176.94` in our setup), which is **different** from the bridge IP we put in `IBKR_HOST` (`172.23.176.1`). With only `127.0.0.1` on the trusted list, Gateway dropped the handshake even though the OS had already accepted the SYN.

**Fix**: Find the WSL VM's peer IP via `netstat -an | findstr ":4002"` (look at the established-connection row, not just the listening row), or via `Get-NetNeighbor` for the WSL adapter. Add that IP to Gateway's Trusted IPs. Restart Gateway (settings only apply on restart).

**Two IPs, two different roles**:
- `IBKR_HOST=172.23.176.1` — the **destination** Windows-side address the container connects *to*. This is the bridge gateway from the container's view.
- `172.23.176.94` (or whatever your WSL distro got) — the **source** the Windows host sees the inbound connection coming *from*. This is what Gateway's allowlist filters on.

Confusing the two costs hours. `127.0.0.1` only covers native Windows processes connecting to Gateway; any container/VM/remote process needs its own peer IP added.

### 3. Compose env-var changes need `--force-recreate`, not just `restart`

**Symptom**: Edited `.env`, ran `podman compose restart python-service`, the running uvicorn still had the old env.

**Root cause**: `restart` reuses the existing container; only `up -d --force-recreate` (or `down` + `up -d`) reads the compose file and `.env` again from scratch.

**Workflow**: any `.env` change → `podman compose down && podman compose up -d`. The "compose restart" muscle memory from Docker doesn't apply for env changes.
