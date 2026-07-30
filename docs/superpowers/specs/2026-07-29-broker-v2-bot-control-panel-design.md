# Broker-v2 Bot Control Panel & Bots List (Alpaca-first) — Design

- **Date:** 2026-07-29
- **Status:** Draft for user review. Deliverable of this cycle = this spec + a PRD. Implementation is sliced separately.
- **Inputs:** operator brief (2026-07-29) + 26-point decision review resolved the same day. The decision register (§20) maps every resolved question to its section.
- **Relation to prior work:** builds on the broker-v2 contract (ADR 0032), the Alpaca phase-1/phase-2 clerk (issues #1170–#1177, #1260–#1264), the OperatorBlocker contract, and the Button Rule from PRD #974. IBKR pages are untouched; IBKR migrates onto this template in the ADR 0032 phase-4 strangler.

## 1. Purpose

A broker-generic bots list and bot control panel, designed as the **target panel for order-producing bots**, shipped Alpaca-first. Today's broker-v2 bots are `mode="log_only"`; the panel degrades honestly for them (§6) — it never renders decorative empty trade/P&L sections. A prerequisite evidence slice (§9) makes decisions and executions durable before the panel claims to display them.

Two lenses, one page:

- **Trader lens** — simple enough for any online trader: what is the bot doing, what did it trade, what is it worth. Chart and trades are first-class, not buried.
- **Operator lens** — a direct, honest representation of the pipeline's real seams and gates, in a deliberately small vocabulary.

Everything is backend-authored; Angular renders. Every routine workflow is UI-operable (§16).

## 2. Scope and non-goals

**In scope:** routes + pages (§3), broker panel capability profile (§4), bots list (§5), trader lens (§6), operator lens (§7), dual-pane chart with bounded history contract (§8), durable decision-receipt stream (§9), P&L contract (§10), presented-actions contract (§11), lifecycle semantics (§12), operator language + manual (§13), evidence access + audit (§14), temporal + scale requirements (§15), no-CLI scope (§16).

**Non-goals:** Alpaca-native live bars (phase-3 `BrokerBarStreamPort`), IBKR migration onto this template (phase 4), a unified cross-broker roster, SSE transport (v1 polls; §7), live (non-paper) trading, a per-user authentication system (§14 states the interim posture honestly).

## 3. Routes and information architecture

Account scope is designed in now — hiding account identity inside configured credentials becomes a routing and safety problem the day a broker exposes a second account.

- `/brokers/:broker/accounts/:accountId/bots` — bots list.
- `/brokers/:broker/accounts/:accountId/bots/:sid` — bot control panel.
- `/brokers/:broker/manual` — full standalone operator manual (§13).
- `accountId` = `BrokerAccountSnapshot.account_id`. For single-account brokers, `/brokers/:broker/bots` redirects to the canonical account path. Deep links are stable across credential rotation.
- The `{broker}` segment resolves via the existing broker registry (unknown broker → typed 404), exactly as `/api/brokers/{broker}/...` does today.
- **API account scope:** the new read/projection endpoints are account-scoped (`/api/brokers/{broker}/accounts/{account_id}/...`) and validate `account_id` against the broker's account; mismatch → 404. Existing non-scoped endpoints (`POST /api/brokers/{broker}/bots`, clerk routes) keep working for the single-account case and gain scoped aliases in slice 1 — no breaking rename.

## 4. Broker panel capability profile

A future broker gets these pages with **zero Angular changes only if** it implements the panel capability profile; otherwise the honest claim is "shared components, minimal adapter work."

- `GET /api/brokers/{broker}/panel-profile` → closed descriptor extending the existing `BrokerCapabilities`: which stations apply (§7), whether flatten is supported, fee-reporting fidelity (§10), live-bar availability (§8), supported presented-actions (§11).
- Angular renders strictly from the profile: an inapplicable station renders as *not applicable* (§7's fifth state), an unsupported action never renders at all.
- The profile is contract-tested per broker (snapshot parity, same discipline as `operator-reason-codes`).

## 5. Bots list — `/brokers/:broker/accounts/:accountId/bots`

**Layout:** account strip on top, roster below. No page scroll at ≥1440×900 (§15); the roster scrolls internally.

**Account strip:** equity · cash · buying power · `PAPER` badge · reconciliation verdict chip · hold banner (if set) · channel-health dots (market data, execution). The list page is self-sufficient: account truth without visiting the desk.

**Roster:** designed and tested for **≥100 bots per account**.

- Columns: Bot · Symbol · Status · Exposure · Fills today · Realized P&L today · Open P&L · Last activity.
- **Attention-first sorting** by default (bots with holds, blocked stations, unclean duty outcomes, or stale evidence float to the top), then search/filter (name, symbol, status), sticky headers, virtualized rows.
- Status labels come from the closed vocabulary (§13): **Working / Off duty / Retired**, plus an attention marker (icon + text, not color alone).
- Row actions: **Start** (off duty), **Stop** (working) — both via the presented-actions contract (§11). Row click → control panel.
- Global actions: **Deploy bot** (dialog → existing `POST /api/brokers/{broker}/bots`), **Refresh**.

**Backend:** `GET /api/brokers/{broker}/accounts/{account_id}/bots/catalog` → `BotCatalogView[]` = `BotStatusView` + rollups (`exposure`, `fills_today`, `realized_pnl_today`, `open_pnl`, `last_activity_at_ms`, `needs_attention`, `status_label`). Rollups are **maintained incrementally on journal append** (per-bot rollup cache) — never a full journal scan per request (§15).

## 6. Bot control panel — trader lens (default)

```
┌ EMA-Cross · SPY · ● Working ──────────────────────────[ Stop ]┐
│ "Watching 1-minute bars. Last decision 10:42 — no entry."     │
├───────────────────────────────┬───────────────────────────────┤
│ LIVE (today, NY) — IBKR   [⛶] │ HISTORY — Polygon         [⛶] │
│ candles + this bot's fills    │ [1D 5D 1M 3M 1Y All] + fills  │
├───────────────────────────────┴───────────────────────────────┤
│ TRADES TODAY (NY trading date · RTH policy shown)             │
│ 10:31 BUY 100 @ 512.30 → 10:58 SELL 100 @ 512.90              │
│       realized +$60.00 · Fees not reported · held 27m         │
├───────────────────────────────────────────────────────────────┤
│ Realized today +$60.00 · Open P&L +$0.00 · Exposure 0 · WO 0  │
└───────────────────────────────────────────────────────────────┘
```

- **Headline** — one backend-authored sentence sourced from the latest **decision receipt** (§9), never from logs.
- **One primary verb** — Start or Stop (semantics in §12), via presented actions (§11).
- **Trades are first-class** — markers on both chart panes *and* a paired entry/exit trades list directly under the chart. Realized and open P&L are shown **separately, never merged** (§10). Missing fees render as **"Fees not reported"**, never `$0.00`.
- **Log-only bots:** the trades/P&L region is replaced by a single honest panel — *"This bot observes and decides but does not place orders (log-only). Decisions appear below."* — with the decision-receipt tail instead of trades. No empty tables.
- Trader lens shows **summarized receipts only**; raw broker evidence stays behind the operator lens (§14).

## 7. Bot control panel — operator lens

The operator lens shows the pipeline as it actually is. Four regions:

**7.1 Transaction rail (center).** The six-station rail renders **one selected transaction** (order intent), not the whole bot — a pipeline is a property of a transaction; a bot is a fleet of transactions. Default selection: most recent intent; any journal-tail row selects its transaction.

```
SIGNAL → INTENT → SUBMIT GATE → BROKER ACK → FILL → RECONCILED
```

Each station has **five states**, each rendered as icon + text + color (color never alone):

| State | Meaning |
|---|---|
| `satisfied` | station completed with evidence |
| `waiting` | expected to progress; nothing wrong |
| `blocked` | an identified condition prevents progress (carries an `OperatorBlocker`) |
| `unknown_stale` | evidence exists but is too old to trust; freshness threshold shown |
| `not_applicable` | this broker/mode has no such station (from the panel profile, §4) |

Station receipts are backend-authored: what happened, when (ms UTC, rendered by the shared timestamp component), and an evidence link (operator-gated, §14).

**7.2 Bot health card (beside the rail, not inside it).** Phase, desired state, duty outcome when off duty (kind + backend-authored reason), decision-receipt freshness, last bar seen. Terminal action: **Retire** (§12).

**7.3 Account/clerk card (beside the rail).** Hold state, reconciliation verdict + last sweep time, outstanding uncertain intents, channel health. Actions: **Reconcile now** (new endpoint), **Clear hold** — enabled only when the hold's root condition is healthy **and freshly observed**; the confirmation shows the account-wide blast radius (every bot on the account). No force-override path on this button; if a force path is ever needed it gets its own design.

**7.4 Journal tail (bottom, internal scroll).** Order-journal entries newest-first, filterable by kind; each row expands to its summarized receipt and (operator-gated) raw evidence, and selects its transaction on the rail.

**Transport:** v1 polls `GET /api/brokers/{broker}/accounts/{account_id}/bots/{sid}/panel` every 5s (everything except chart data, including presented actions and their `revision`). SSE is a deliberate later upgrade.

## 8. The dual-pane chart

Two independent `lightweight-charts` instances (independent timescales make the panes API wrong here) in one shared `DualPaneMarketChartComponent`. **Two-column composition on desktop** (side by side), stacked on narrow viewports; each pane has a **full-screen expand** control.

**LIVE pane** — today's NY session for the bot's symbol from the **IBKR live strain** (`LiveBarAggregator`), with this bot's fill markers. When live bars are unavailable (feed down, pre-subscription, closed session), the pane fills with **Polygon bars in the existing greyed inactive style** plus an honest chip: *"Live feed unavailable — showing Polygon (delayed)."* Source provenance is truthful and rendered: bars stay tagged `ibkr` / `polygon` / `mixed` (the shading already exists in `candleDataForBar()`).

**HISTORY pane** — Polygon, **bounded presets: 1D · 5D · 1M · 3M · 1Y · All**, each mapped server-side to a fixed aggregation ladder (1D→1m, 5D→5m, 1M→30m, 3M→1h, 1Y→1d, All→1d). Fill markers across the bot's whole life at the aggregated resolution. Never a lifetime of 1-minute bars in one response.

**Backend:**

- `GET .../bots/{sid}/chart/live` — today's merged source-tagged bars + today's fill markers; polled ~5s. Reuses `live_chart_window` internals.
- `GET .../bots/{sid}/chart/history?preset=…` — **a new bounded history contract.** The existing resolver is explicitly capped at 7 days (`validate_chart_window`, `app/services/live_chart_window.py`) and is **not widened**; the new endpoint owns preset→aggregation mapping and response-size bounds.
- Fill markers project from the clerk order journal's fill events filtered by the bot's namespace (`learn-ai/{sid}/v1:*`) via a new `project_instance_fills(sid)`, parity-tested against the trades list (§10).

**ADR amendment (required):** amend ADR 0032 to record **IBKR as a time-boxed signal-feed bridge** for broker-v2 bots — market-data strain and execution broker are distinct concerns; Polygon remains **display fallback only** (never silently promoted to a signal source). The bridge retires when phase-3 `BrokerBarStreamPort` lands.

## 9. Decision-receipt stream (prerequisite slice)

Logs are not evidence. Before the panel ships, bots gain a **durable per-bot decision journal** (append-only JSONL, same fsync discipline as the order journal): one receipt per bar evaluation — `{ts_ms, bar_ref, outcome, reason_code, indicator_snapshot}` where `outcome ∈ {entered, exited, no_action, blocked}`. It feeds the SIGNAL station, the trader headline, and decision freshness on the bot health card. Bounded read API (tail + by-transaction), incremental rollups for the catalog.

## 10. P&L contract

- **Realized P&L:** **FIFO lot accounting over exactly attributed fills** (the bot's namespace only — never account-net), supporting partial closes and reversals. Method disclosed in an info tooltip and the manual.
- **Open P&L:** attributed open lots × current price, always displayed **separately** from realized. Labeled *indicative* (marks are not authoritative).
- **Fees:** rendered only when the broker reports them; otherwise **"Fees not reported"** — never `$0.00`.
- **Rigor:** one canonical implementation in `PythonDataService` with a provenance block and a golden-fixture test (partial fills, reversals, multi-day positions); the trades list, catalog rollups, and chart markers all derive from it (`learn-ai-validation` applies).

## 11. Presented-actions contract

The backend presents actions; Angular executes only a **closed set of known action ids** and renders exactly what it is given.

```
PanelAction {
  action_id: closed enum (deploy, start, stop, flatten_stop, retire,
             cancel_order, clear_hold, reconcile_now),
  label, explanation: backend-authored,
  enabled: bool,
  blockers: OperatorBlocker[],          # reused contract, not a new one
  confirmation: {required, prompt, ack_phrase?} | null,
  revision: int                          # panel-state revision this action binds to
}
```

- Execution posts `{action_id, revision, idempotency_key, reason?}`. Stale revision → `409` + surface refresh; the idempotency key makes double-clicks and retries safe.
- Unknown `action_id` from the server renders as disabled with its explanation (forward compatibility without fake buttons).
- **Operator identity is never a request text field.** Identity derives from the authenticated control channel (§14); forms carry only the reason.

## 12. Lifecycle semantics

- **Stop** — stop signal evaluation and **cancel bot-owned working entry orders** (namespaced, via the clerk), but **leave exposure visible**. Stop never silently flattens.
- **Flatten & stop** — separate, operator-lens-only action: cancel working orders, submit closing order(s) through the clerk, then stop. Confirmation states exactly what will be sold/bought.
- **Retire** — terminal. A retired `strategy_instance_id` is **never reused**; replacement deploys a new sid carrying `replaces_sid` lineage. Confirm dialog per Button Rule.
- **PAUSED is removed from this surface.** Verified dead: zero references in `bot_runner.py`; it is inherited vocabulary from the shared `desired_state` literal and unreachable for broker-v2 bots. This surface's contract narrows `desired_state` to `RUNNING | STOPPED`, with a contract test pinning that the panel never receives `PAUSED`. (Precedent: PRD #974 rev 3 deleted pause from the daily lifecycle.) The removal is explicit here — not an invisible collapse into Off duty.

## 13. Operator language and the manual

- **One closed vocabulary** (~25 codes): 3 phases, 7 duty-outcome kinds, ~4 hold reasons, 4 reconciliation verdicts, 3 channel states, 6 station ids, 5 station states, 8 action ids. Python authority + snapshot + TS parity test — the existing `operator-reason-codes` mechanism.
- **Backend-authored prose is the sole semantic-copy authority.** The TS copy map exists only as an explicit emergency fallback; a **visible contract test fails when the server omits required copy** (label/explanation) for any emitted code. No open-ended string reaches the UI unlabeled.
- **Manual:** `docs/broker-v2-operator-manual.md`, rendered at `/brokers/:broker/manual`. Page one is the six-station diagram; then one page per station, a button-by-button reference (every presented action), and a glossary. Deliberately ~⅕ the IBKR manual's size because the surface is genuinely smaller.
- **Card help never navigates away:** each card's "?" opens an **anchored side drawer** with that card's manual section; operational context stays visible.

## 14. Evidence access, identity, and audit

- **Trader lens:** summarized receipts only.
- **Operator lens:** raw broker evidence via dedicated endpoints that are **bounded** (paged, size-capped), **redacted** (the capture journal already strips secrets; responses re-verify), timestamped, and **audit-logged server-side** (who/when/what evidence was read).
- **Honesty note:** trader/operator is a presentation lens today, not an enforced permission boundary — there is no per-user auth system. The spec does not pretend otherwise. Evidence endpoints are gated to the operator surface and audit-logged now; true per-user authorization arrives with a real auth system.
- **Operator identity, interim posture:** control mutations authenticate via the service-level control secret; the server attaches a **configured operator identity** to journaled actions. No free-text identity fields anywhere in the UI.

## 15. Temporal, viewport, and scale requirements

- **"Today" = the canonical New York trading date** from the calendar module — never browser-local midnight. The bot's RTH/extended-hours policy is displayed alongside. All wire timestamps are `int64 ms UTC`; rendering goes through the shared timestamp component (temporal-rigor applies unchanged).
- **Viewport:** no page scroll at **≥1440×900**; compact mode below that; mobile stacks and scrolls normally. Internal scrolling is expected in the roster, trades list, journal tail, and evidence drawer.
- **Scale:** designed and perf-tested at ≥100 bots/account. Catalog and panel projections read incremental rollups; nothing does O(journal) work per request.

## 16. Scope of "100% UI-operable"

Guaranteed for **routine workflows**: bot lifecycle (deploy/start/stop/flatten-stop/retire), orders (submit escape hatch, cancel), holds (clear with prerequisites), reconciliation (view + reconcile-now), and evidence review. **Excluded:** credential rotation, host outages, corrupted artifacts, infrastructure repair — these may require external administration, and when they bite, the UI says exactly what is wrong and where the external procedure lives (manual link), rather than presenting a dead button.

## 17. Testing

- **Python:** journal-fixture-driven tests for every projection (catalog rollups, panel, fills, chart live/history bounds); golden-fixture FIFO P&L tests (partial fills, reversals); vocabulary snapshot parity; presented-actions revision/idempotency tests; a ≥100-bot synthetic fixture for scale assertions.
- **Frontend:** Testing-Library rendered-output tests per component; contract test that fails visibly when server copy is missing; AXE checks (five-state stations must pass icon+text, not color-only).
- **Cross:** fills↔trades↔markers parity test (one canonical P&L source); contract test pinning `PAUSED` absence.

## 18. Slices (PRD input)

0. **Evidence prerequisite** — decision-receipt journal, `project_instance_fills`, canonical FIFO P&L + golden fixtures, incremental rollup caches.
1. **Contracts & projections** — panel-profile, catalog, panel, presented-actions (+ reconcile-now, retire, stop/flatten-stop semantics), chart live + bounded history endpoints, vocabulary snapshot, ADR 0032 amendment.
2. **Bots list page** — account strip, roster (scale + attention-first), deploy dialog.
3. **Trader lens** — dual-pane chart component, trades-today list, headline + primary verb, log-only degradation.
4. **Operator lens** — transaction rail, health/clerk cards, journal tail, evidence drawer + audit.
5. **Manual & language** — manual content, anchored help drawers, copy-fallback contract tests.

## 19. Out of scope (recorded)

Alpaca-native live bars (phase 3), IBKR migration (phase 4), unified cross-broker roster, SSE transport, live-mode trading, per-user authentication, force-override for holds.

## 20. Decision register

| # | Question | Resolution | § |
|---|---|---|---|
| 1 | Log-only or order-producing target | Order-producing target; honest log-only degradation; evidence prerequisite slice | 1, 6, 9 |
| 2 | IBKR signal-feed bridge | Yes, time-boxed, ADR 0032 amendment; Polygon display-fallback only | 8 |
| 3 | Zero-Angular-change claim | Only via panel capability profile; else "minimal adapter work" | 4 |
| 4 | Multi-account routing | Account-scoped routes now | 3 |
| 5 | Rail scope | Per-transaction rail; health/reconciliation beside it | 7 |
| 6 | Decision receipts | Durable decision journal; logs are not evidence | 9 |
| 7 | Copy authority | Backend prose sole authority; TS fallback + failing contract test | 13 |
| 8 | Stop semantics | Stop = signals + cancel bot-owned entries, keep exposure; separate Flatten & stop | 12 |
| 9 | Sid reuse | Never; `replaces_sid` lineage | 12 |
| 10 | PAUSED | Removed from surface (verified dead in `bot_runner.py`); contract-test pinned | 12 |
| 11 | Viewport | No page scroll ≥1440×900; compact below; mobile scrolls | 15 |
| 12 | Internal scrolling | Accepted in roster/trades/journal/evidence | 15 |
| 13 | Fleet size | ≥100 bots; attention-first, search, sticky headers, no journal scans | 5, 15 |
| 14 | "Today" | Canonical NY trading date; policy displayed | 15 |
| 15 | Lot accounting | FIFO over attributed fills; method disclosed | 10 |
| 16 | Realized vs open | Always separate | 10 |
| 17 | Missing fees | "Fees not reported", never $0.00 | 10 |
| 18 | Station states | Five states; icon+text+color | 7 |
| 19 | Presented actions | Server-presented, revision-bound, idempotent, closed set | 11 |
| 20 | Clear hold | Prerequisites healthy + fresh; blast radius shown; no smuggled force | 7 |
| 21 | Operator identity | Derived from authenticated channel (configured identity interim); reason-only forms | 11, 14 |
| 22 | Chart history | Bounded presets + aggregation ladder; new contract (7-day resolver not widened) | 8 |
| 23 | Pane composition | Two instances, two-column desktop, per-pane fullscreen | 8 |
| 24 | No-CLI scope | Routine workflows only; infra external with UI explanation | 16 |
| 25 | Card help | Anchored side drawer; standalone manual remains | 13 |
| 26 | Raw evidence access | Operator-gated, bounded, redacted, audit-logged; lens honesty note | 14 |
