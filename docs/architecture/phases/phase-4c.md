# Phase 4c progress (2026-05-17, follow-up PR — accept arbitrary algorithm source)


After Phase 1c promoted `--read-only` and `--user=<non-root>` to mandatory sandbox flags, the
`POST /lean/runs/start` API and the LEAN Lab UI both accept arbitrary `QCAlgorithm` source from
the operator. Phase 1c's hardening is the precondition that makes accepting arbitrary source
acceptable.

- **API** — `TrustedRunRequestModel.algorithm_source: str | None`. Empty/whitespace is rejected
  with HTTP 422 (better signal than a silent fallback). UTF-8 size validated against
  `MAX_ALGORITHM_SOURCE_BYTES = 256 KiB`. Omitting the field falls back to the bundled trusted
  `buy_and_hold.py`. `extra="forbid"` still rejects unknown fields.
- **Service** — `TrustedRunRequest.algorithm_source` flows through to `stage_algorithm_source(...)`;
  the manifest gains `algorithm_source_kind={user_provided|trusted_sample}` so the audit trail
  records intent. Class name MUST be `MyAlgorithm` (matches `algorithm-type-name` in
  `LeanConfig`) — a mismatch causes LEAN to run its image-baked default and the run looks
  "successful" with empty output.
- **UI** — `lean-lab.component`: new Reactive Forms controls `useCustomAlgorithm: boolean` +
  `algorithmSource: string`. The toggle defaults off (operator still gets a one-click trusted
  run); turning it on reveals a monospace textarea pre-populated with a minimal `MyAlgorithm`
  template that runs against the sample data window. Whitespace-only source is silently omitted
  client-side rather than sent for a server 422.
- **Sandbox guarantee surfaced in UI copy** — header explicitly names the Phase 1c shape
  (read-only root, non-root user, no caps, no network, workspace-only mount) so the operator
  knows what protects the host when they paste arbitrary code.
- **Test surface** — 4 new router tests (`test_algorithm_source_optional`,
  `..._empty_string_rejected`, `..._oversize_rejected`, `..._within_cap_accepted`); 1 new
  field-rejection test renamed from the stale Phase 2a `test_forbids_algorithm_source_field`.
  3 new component specs cover (a) toggle-off omits the field, (b) toggle-on with source sends
  it, (c) toggle-on with whitespace-only omits it.
- **What Phase 4c does NOT do** — does not run arbitrary algorithms through the
  reconciliation-grade path; does not stage additional brokerage / fill model variants
  (still LEAN defaults); does not relax the trusted-sample data window or stage real factor
  / map files. Those remain Phase 5+.
