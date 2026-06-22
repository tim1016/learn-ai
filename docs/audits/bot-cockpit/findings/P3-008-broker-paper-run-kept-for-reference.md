# P3-008 — `broker-paper-run/` legacy directory kept for reference (no action)

## Where

- `Frontend/src/app/components/broker/broker-paper-run/`
- `Frontend/src/app/app.routes.ts:214-221` — redirect `/broker/paper-run` → `/broker/instances`

## Severity

**P3** — audit-only finding. Not actionable per run-prompt §7.3 ("no 'I think this is unused' deletes").

## Observation

The `broker-paper-run` directory is the v1 run-spine paper-run page that PRD #565 / ADR-0004 replaced with the instance-addressed cockpit (`broker/instances`). The route table makes the retirement explicit:

```ts
{
  // Cutover (#400): the run-spine paper-run page is retired; the
  // instance-addressed control room is the operator console. The old
  // component is kept for reference but no longer routed.
  path: "broker/paper-run",
  redirectTo: "broker/instances",
  pathMatch: "full",
},
```

The comment "kept for reference" expresses deliberate retention, so this audit does not delete it.

## Status

Audit-only. No fix.
