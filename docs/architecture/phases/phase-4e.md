# Phase 4e progress (2026-05-17, follow-up PR — form rehydration from manifest)


Phase 4d added the sidebar but a click only repopulated the result
panel; the form fields stayed at their defaults, so re-running a past
configuration meant re-typing it. Phase 4e closes that loop.

- **No new endpoint.** `GET /api/lean-sidecar/runs/{id}/manifest`
  has existed since Phase 2a; Phase 4e just adds a typed frontend
  wrapper (`LeanSidecarService.getManifest`) that returns a narrow
  `RunManifest` TS interface — only the fields the form needs are
  typed; the rest of the dict passes through as `unknown`.
- **`rehydrateFormFromManifest` policy.** Symbol, starting cash,
  and the requested window come from `manifest.parameters` and
  `manifest.requested_window_ms`. The algorithm source is NOT
  rehydrated — the manifest stores only its sha256 (provenance hash),
  not the source itself. The toggle resets to off; operators re-running
  a user-source algorithm re-paste it. A fresh `runId` is generated
  so a re-run with the rehydrated form lands in a new workspace
  (mixing artifacts in the same dir would corrupt the audit trail).
- **Defensive wire-type coercion.** `starting_cash` is serialized as
  a string by the trusted-sample staging code and as a number
  elsewhere; the rehydrator accepts both. Out-of-range cash (below
  the $1k server min) is rejected from the patch entirely rather
  than auto-clamped — patching it in would immediately invalidate
  the form, and the operator is better served seeing the old value
  with a fresh symbol/window than seeing the form go red on click.
- **Manifest fetch failure is non-fatal.** A 404 (legacy run with
  no manifest written) leaves the form at its previous values; the
  result panel still renders. A swallow-with-comment is the right
  call here — surfacing every 404 in the UI for the legacy-run case
  would be noise without a remediation action.
- **Test surface** — 4 new component specs (full rehydration, numeric
  starting_cash variant, 404 leaves form intact, below-min cash
  rejected from patch); 2 new service specs (getManifest success,
  getManifest 404 envelope). 39 frontend tests pass (was 33).
