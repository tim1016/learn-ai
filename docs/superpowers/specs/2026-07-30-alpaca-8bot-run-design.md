# Alpaca 8-bot UI-driven paper run — design (2026-07-30)

Goal: eight Alpaca paper bots concurrently ON_DUTY and producing real orders for
at least 15 minutes (target 30–45), on paper account PA3KWXU1C4C3, with every
deploy and stop performed through the broker-v2 panel UI. First-ever Alpaca
trades for this platform. Orchestration: Fable directs; Sonnet agents build,
operate, and watch.

## Decision register (operator-locked, 2026-07-30)

| # | Decision | Choice |
|---|---|---|
| D1 | Close the log-only gap | Build order-producing `trade` mode today, before the run |
| D2 | Live bar source | Reuse the existing IBKR feed (no Alpaca-native feed today); IB Gateway health becomes a preflight + monitored dependency |
| D3 | Strategy | `DeploymentValidationConsecutiveGreen` signal logic (2 consecutive green 1-min bars → long; exit 3 bars later; window open+15m → close−15m), adapted to fixed-share sizing |
| D4 | Sizing | Fixed `quantity=1` share per bot (engine version's `set_holdings(1.0)` would 8× over-allocate one account) |
| D5 | Run style | UI-driven: all state-changing actions through the panel; read-only API polling allowed for monitoring/evidence |

## Success criteria

1. ≥15 min with all 8 bots concurrently running (clock starts when bot 8 is ON_DUTY).
2. Every bot submits ≥1 real order that fills on Alpaca paper.
3. Account ends flat (positions 0, open orders 0) with a clean reconciliation verdict.
4. Evidence report written to `docs/audits/alpaca-8bot-run-2026-07-30.md`.

## Known state this design builds on (verified 2026-07-30 ~11:10 ET)

- Runtime green: Alpaca paper keys in container env (`ALPACA_MODE=paper`),
  account reachable (equity ~$99,998, trading not blocked), clerk in-process and
  healthy, panel routes live, all containers up.
- Gap: bots are log-only (`_run_log_only_bot` never calls
  `AlpacaClerk.submit_for_instance`, which is built and tested). Bot bar feed is
  the IBKR feed; deploy 503s if IBKR is down.
- Panel actions wired end-to-end today: deploy (dialog), stop, reconcile_now,
  clear_hold. Stubs returning 409: start, retire, flatten_stop, cancel_order.
  Stop keeps exposure (locked panel decision).
- Sidebar "Bots" link hits the unscoped route with `accountId=undefined` → broken
  catalog fetch. Account-scoped URL works.

## Phases

### P1 — Build (two Sonnet builders, parallel worktrees; Fable reviews and merges)

- **Builder-PY**: `mode: Literal["log_only","trade"]` + `quantity: int` across
  `BrokerBotBinding` / `DeployBotRequest` / `BotStatusView`; new `_run_trade_bot`
  with the D3 signal state machine, all orders via `clerk.submit_for_instance`,
  bot-owned order-identity namespace so panel fill attribution works; detection
  window derived from the canonical calendar module (no hardcoded session
  times — temporal-rigor ban list); window-end flatten; submit failure → surface
  and stop entering (no retry storm). Unit tests (fake feed + fake clerk).
  Regenerate committed OpenAPI contract (+ broker.types.ts codegen).
  Sizing + hold-from-submit divergences from the engine reference documented in
  the module docstring (numerical-rigor rule 4).
- **Builder-FE**: unscoped bots route resolves the default account (fetch
  account id → redirect to scoped route) so the sidebar link works; deploy
  dialog gains mode select + quantity input wired into the POST body. Vitest specs.
- Merge gates: project-scope ruff, broker/bot pytest suites, eslint
  `--max-warnings 0`, Vitest. No PR today; thermo runs when this branch is
  eventually pushed as a PR.

### P2 — Deploy to runtime

Merge to `codex/feat-alpaca-bot-navigation` in the main checkout →
`podman restart polygon-data-service` (hot reload is off by design) → frontend
recompiles via `ng serve --poll` → hard browser refresh.

### P3 — Preflight (Sonnet checker; Fable signs off)

IB Gateway up and IBKR feed enabled in the container; Alpaca account flat, no
open orders, no exposure hold, both clerk channels healthy; trade_updates
websocket connected; restart-intensity headroom clean.

### P4 — Run (UI-driven; Sonnet UI-operator + Sonnet watcher)

Canary first: deploy SPY qty 1 via the panel dialog, confirm bars flow and the
first order cycle completes (~5 min). Then deploy the remaining 7 staggered
30–60 s: QQQ, AAPL, MSFT, NVDA, AMD, AMZN, META (qty 1, use_rth=true, sids
`alp8-<symbol>-0730`). Watcher polls read-only endpoints (bots list, clerk
status, orders, positions) every 60–90 s plus periodic panel screenshots and
reports to Fable. Alert triggers: bot task death, clerk hold, uncertain submit,
feed stall >3 min, IBKR disconnect.

### P5 — Wind-down (UI-driven, flat-first)

Stop each bot via the panel when it is between cycles (flat); if in-position,
wait ≤5 bars for the natural exit. Residual positions: close via the manual
page (still UI). Backstop: the strategy's own close−15m flatten. Verify flat +
clean reconciliation verdict.

### P6 — Evidence

Watcher archives per-bot order/fill lists (ids, int64 ms timestamps), clerk
verdicts, flat confirmation, screenshots, decision receipts. Fable writes
`docs/audits/alpaca-8bot-run-2026-07-30.md` and updates memory, including panel
gaps exercised (409 stubs, sidebar route).

## Risks

- IBKR feed dependency (D2, chosen eyes-open): gateway death mid-run stalls
  bars; blast radius ≤1 share per bot. Mitigation: canary, watcher alerts.
- Clerk single-writer serializes simultaneous submits — acceptable at 8 bots.
- Restart-intensity gate (3 starts/5 min per bot) — space any redeploys.
- Timeline: build must gate by ~13:15 ET to keep the fleet window ahead of the
  close−15m flatten backstop.

## Out of scope today

Alpaca-native bar feed; wiring start/flatten_stop/retire/cancel_order action
stubs; any PR/push.
