# Broker Instance Operator Surface — Runbook

Status: shipping with #565 PR 13 (cleanup).  
Audience: operators (traders) running paper / live bots from `/broker/instances/:id`.  
Engineer view: keep this in lockstep with the per-card disclosures and the bottom Audit & Diagnostics accordion.

## What this page answers

The page is reorganized around five questions a trader actually asks during an incident, in decision priority order:

1. **Can it trade?** — readiness verdict, gates that are not passing
2. **Is it safe to let it trade?** — last session ended cleanly vs. fatal halt / poisoned
3. **What is it holding right now?** — current risk, positions, pending orders, daily cap
4. **What did it just do?** — chart, latest signal strip, recent trades
5. **What do I need to fix?** — recent incidents, audit & diagnostics

Anything that doesn't help answer one of those questions lives behind a disclosure or in the Audit & Diagnostics accordion.

## Layout — top to bottom

| Section | Component | What it answers |
|---------|-----------|-----------------|
| Fleet header | `<app-fleet-header>` | Fleet-wide account state (PAPER pill, IBKR connection, contamination verdict, account safety actions) |
| Tab strip (≥2 bots) | inline `<nav class="tab-strip">` | Switch between deployed bots; stable deployment order |
| Sticky control bar | `<app-sticky-control-bar>` | Bot identity + state pill + readiness pill + poison chip; persists while scrolling |
| Hero header | inline `<header class="hero">` | Strategy name + paper-mode banner + hero status |
| Provenance / Audit-trail (PR4) | `<app-audit-trail-accordion>` | Engineer-mode disclosure (run_id, commit, contract SHA, runtime config) |
| Sizing | `<app-broker-sizing-card>` | Sizing policy, governed_by, per-trade audit |
| Strategy Rules (PR7) | `<app-strategy-rules-card>` | Strategy / order mode / daily cap / sizing summary; Redeploy with new rules; Show advanced |
| Current Risk (PR9) | `<app-current-risk-card>` | Posture (Flat/Long/Short/Mixed), open positions, pending orders, daily cap, sizing |
| Last Session (PR10) | `<app-last-session-card>` | Thin stub on clean exit; full card with fix on dirty exit |
| Readiness (PR11) | `<app-readiness-card>` | "READY · N checks pass" strip or BLOCKED/DEGRADED/UNKNOWN full card |
| Pre-Trade Checklist | inline `<section class="card checklist-card">` | Detailed per-gate affordances (button / nav-link / read-only note) |
| Start / Stop card | `<app-broker-start-stop-card>` | Start / Stop controls (source of truth for safety-critical controls) |
| Chart card | `<app-bot-trade-chart-card>` | OHLCV candles + trade markers |
| Latest Signal strip (PR8) | `<app-latest-signal-strip>` | Signal pill + descriptor-backed decision fields below the chart |
| Recent Trades (PR5) | `<app-bot-trades-table>` | Closed entry/exit pairs |
| Recent Incidents (PR6) | `<app-incidents-panel>` | Categorized incidents with operator-language copy + raw-log drawer |
| Bot Behavior | inline | Intent setting (PAUSE / RESUME / STOP) |
| Strategy State | inline | Decision columns from the engine |
| Broker / Managed Positions | inline | Namespace-attributed positions (deprecated overlap with Current Risk — see Follow-ups) |
| Advanced Actions | inline | FLATTEN, MARK_POISONED, paper reset (deprecated overlap with sticky kebab — see Follow-ups) |

## Honest contracts

- **Timestamps** — wire and storage are `int64 ms UTC` end-to-end. Display-side strings render in `America/New_York` for the operator. See `numerical-rigor.md` § Timestamp rigor.
- **Position posture** — Current Risk (PR9) filters zero-qty entries out before deciding `Flat / Long / Short / Mixed`. A residual stale entry can't flip the posture silently.
- **Daily cap** — the count is read verbatim from `readiness.gates` where `name === 'orders_cap'`. When the engine has not emitted a typed cap, the card says so honestly ("Daily cap status not reported by the engine") — no fabricated counter.
- **Incidents** — backend `IncidentCategory` enum + `parse_incidents` classifier (#565 PR 1, #566) is the single source of truth. The frontend `INCIDENT_COPY` map (PR6) is operator-language presentation only; unknown categories degrade to "Unknown error — see raw traceback."
- **Readiness** — gate-by-gate detail strings are surfaced verbatim. The Readiness card (PR11) leads with the verdict and proportional count; the existing Pre-Trade Checklist below renders the affordance-per-gate UX.

## What is deferred

The 13-PR sequence intentionally defers the following beyond #565:

- **Sticky bar destructive kebab** (User Stories #31 – #40) — FLATTEN, MARK_POISONED, Reset Paper Account dialogs in the sticky bar. The existing Advanced Actions card continues to drive those flows. Deferring keeps destructive control flow in one place during the operator-first refactor.
- **Sticky bar Restart & Update button** (User Stories #39 – #40) — conditional render based on platform-code freshness, which doesn't have a backend contract yet.
- **Per-gate affordances on the Readiness card** (User Stories #11 – #13) — button / nav-link / read-only note per gate. The existing Pre-Trade Checklist already renders these via the parent's `fixAction()` taxonomy; full extraction to the new card lands after the sticky bar takes ownership of Start / Pause / Stop.
- **Next-evaluation timestamp on the signal strip** (User Story #24) — needs a new backend contract field; today the strip is a no-new-math addition.
- **Stale-data freezing** (User Stories #41 – #44) — the safety-critical-card freeze + dim + control-disable behavior when the daemon hiccups. Needs a backend freshness contract per resource that doesn't exist yet (the current `poll_interval_ms` is a temporary approximation, not a per-resource staleness signal).

## Post-merge cleanup (the actual #565 PR 13 sweep)

Once PRs 4 – 12 land, the following surfaces become reachable-but-redundant and should be removed in a follow-up sweep:

- The inline "Managed Positions" card in `broker-instances.component.html` is now redundant with the Current Risk card (PR9). Remove the inline section and the `brokerPositions()` helper.
- The inline "Latest Strategy Signal" card is now redundant with the Latest Signal strip (PR8). Remove the inline section.
- The static `Why It Stopped` heading was already removed by PR10's switch to `<app-last-session-card>`; double-check the inline last-exit-card block in the parent is gone end-to-end after PR10 merges.
- The legacy `bot-failures-table` folder was deleted by PR6; spot-check no stale imports or routes reference it after PR6 merges.

These are not done in this PR because PR 13 is independently branched from `master` and the redundancies only exist *after* the new components land. The sweep is mechanical and will be a small follow-up once the 12 cards are on `master`.

## Quick visual audit before deploy

After merging PRs 4 – 12, walk the page top-to-bottom on a paper bot:

1. PAPER pill visible in the sticky bar
2. Bot identity + state pill + readiness pill visible
3. Posture chip on Current Risk matches expected positions (Flat for a brand-new bot)
4. Last Session shows thin stub on a clean prior exit, full card with `Re-deploy (fresh run_id)` on a dirty one
5. Readiness card shows the calm strip when verdict is READY
6. Strategy Rules card renders the four primary rows; `Show advanced ▾` reveals the broker address + hydration mode + contract path
7. Latest Signal strip below the chart shows the engine's last decision
8. Recent Incidents renders operator-language copy (not raw `Error 1100`)

If any of those land wrong, the corresponding PR in the series is the place to look first.
