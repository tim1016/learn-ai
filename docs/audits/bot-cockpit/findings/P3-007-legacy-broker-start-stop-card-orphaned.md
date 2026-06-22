# P3-007 — Legacy `broker-start-stop-card` orphaned; deletion authorized by §7.3 protocol

## Where

- `Frontend/src/app/components/broker/broker-start-stop-card/` (deleted by this audit)
- `Frontend/src/app/components/broker/cockpit-v2/reused/host-process-notice/host-process-notice.component.ts:17-31` (doc-comment updated)

## Severity

**P3** — low-impact consistency / dead-code cleanup. Per run-prompt §7.3 ("Legacy cockpit handling: You may delete legacy v1 cockpit code only if you can prove (via route table + git grep + import graph) that nothing live references it"), the deletion is authorized once the proof is recorded.

## Proof of non-reference

| Check | Command | Result |
|---|---|---|
| Route table | `grep -n broker-start-stop Frontend/src/app/app.routes.ts` | no hits |
| Class import | `grep -rn 'BrokerStartStopCardComponent' Frontend/src/` | only its own source + own spec |
| Template selector | `grep -rn 'app-broker-start-stop-card' Frontend/src/` | only the now-deleted file + one doc-comment in `host-process-notice.component.ts` |
| Tests / e2e | `grep -rn broker-start-stop Frontend/tests/ Frontend/angular.json` | no hits |
| Sibling legacy component | `grep -rn broker-start-stop Frontend/src/app/components/broker/broker-paper-run/` | no hits — even the retired-but-route-redirected `broker-paper-run` does not import it |

The doc-comment in `host-process-notice.component.ts:17-31` already named `<app-broker-start-stop-card>` as "deleted" in PRD #607 / Slice 8 / #615 — the source intent was to remove it. The deletion was never executed; this finding closes that gap.

## Why this exists

The component was the v1 cockpit's start / stop primary surface for the host runner. The Slice 8 work that replaced it with `<app-host-process-notice>` (because the host runner is operator-owned per ADR-0003/0007) intended to retire it but the file deletion was skipped.

## Fix

- `git rm -r Frontend/src/app/components/broker/broker-start-stop-card/` (4 files).
- Update `host-process-notice.component.ts` doc-comment: from "the deleted `<app-broker-start-stop-card>`" to "the legacy `<app-broker-start-stop-card>` (removed in the 2026-06-22 cockpit audit; the component had no live references and was already documented as superseded)". The corrected comment makes the historical context explicit and accurate.

## Out of scope (recorded as P3-008 below)

`Frontend/src/app/components/broker/broker-paper-run/` is also legacy — `app.routes.ts:217-221` redirects `/broker/paper-run` → `/broker/instances` and explicitly says "The old component is kept for reference but no longer routed." Per the comment's "kept for reference" intent, this audit does **not** delete it. Captured as P3-008 (audit-only).

## Status

Fixed in this PR. No regression test needed (deletion is the regression).
