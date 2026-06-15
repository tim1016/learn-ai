---
id: VCR-0010
severity: P1
status: partially_remediated
area: ui-runtime-claims
canonical_file: Frontend/src/app/components/broker/broker-instances/**
reference: PRD §12.10, broker-user-manual.html §4
first_seen: 2026-06-14
last_seen: 2026-06-14
remediated_in: "#502 — Phase 7A — Broker safety verdict on /broker/health (verdict surfaced; rendering wired)"
regrounded_to: high
follow_up_required:
  - "Phase 7B — verdict order-blocking + mid-session transition halt + Resume guard"
lens: ui-vs-runtime-claims
dedupe_with_F: none
confidence: high
---

## What

The broker-instances hero displays the static string *"Paper trading mode — no real money at risk"* with **no reactive consultation of the actual broker mode**. The runtime stack defends paper-mode at four enforcement layers (`IBKR_READONLY`, `IBKR_MODE=paper`, port-not-in-`LIVE_PORTS`, `account_id.startswith("DU")`) before `place_paper_order` returns — so the operator cannot accidentally route to a live account today. But the hero label is a **trust anchor**: an operator reading it on a misconfigured deploy (where one or more of the enforcement layers is in an unexpected state) would receive false reassurance even though the runtime is blocking.

The flip case is what makes this P1: a future toggle to live mode (the deploy form already has a "Live mode can place real orders" dialog — see VCR-P3-rollup) would not be reflected in the hero. The hero would continue to say "Paper trading mode — no real money at risk" while the runtime gates allow real orders.

## Where

- `Frontend/src/app/components/broker/broker-instances/**` — hardcoded hero string (specific component and line range to re-verify).
- `PythonDataService/app/broker/ibkr/orders.py::place_paper_order` — the four-layer enforcement stack.
- `PythonDataService/app/config.py` — `IBKR_MODE`, `IBKR_READONLY` settings.
- `docs/broker-user-manual.html` § 4 — claims paper-only as a product property.

## Why this severity

PRD §7 P1: "UI implies guarantees the backend/runtime does not enforce." The runtime does enforce paper-only today, so the practical risk is bounded. But the UI is a trust anchor that promises a guarantee independent of the runtime state — the test in PRD §7 is whether the UI's promise depends on something the operator can verify, and a hardcoded string does not depend on anything.

Not P0 because the runtime is currently the gate. The day a "live mode" path lands (or someone toggles `IBKR_READONLY=false` without retesting), this hero becomes silent corruption of operator expectations.

## Trading impact

- **Today**: false reassurance only — runtime gates hold.
- **Tomorrow** (any live-mode work): the hero continues to claim paper while the runtime is allowing live orders.

## Reproduction

```bash
grep -rn 'Paper trading mode\|no real money at risk' Frontend/src/app/components/broker/
grep -n 'IBKR_MODE\|IBKR_READONLY\|LIVE_PORTS' PythonDataService/app/broker/ibkr/orders.py
```

## Suggested resolution (NOT auto-applied)

Bind the hero label to a server-side `broker.mode` field (reactive signal in the broker status response). When `mode === "paper"` AND `account.startsWith("DU")` AND `readonly === true`, render the green "Paper trading mode" hero. Otherwise render an amber "Mode: live (or non-paper account detected)" hero that names exactly which check failed.

Add a runtime cross-check on the Backend: the GraphQL `getBrokerStatus` resolver consults the actual `IBKR_MODE` + `account` + `LIVE_PORTS` and returns a structured verdict. The hero binds to that verdict, not to a string.

## Provenance of the finding

Lens: `ui-vs-runtime-claims` (workflow `wf_def78013-ce4`). Lens summary identified the hardcoded label; specific component/line range to re-verify before remediation. `medium` confidence pending that.
