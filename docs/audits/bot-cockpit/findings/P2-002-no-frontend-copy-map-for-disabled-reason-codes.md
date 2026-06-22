# P2-002 — No Frontend copy map for server-authored disabled reason codes

## Where

- `Frontend/src/app/components/broker/cockpit-v2/cockpit-shell.component.html:231-287` (action buttons)
- `Frontend/src/app/components/broker/cockpit-v2/tabs/audit-tab.component.html:88-101` (Mark POISONED trigger)
- `Frontend/src/app/components/broker/cockpit-v2/cockpit-shell.component.html:292-298` (Resume expanded reasons)

## Severity

**P2** — confusing operator copy (run-prompt §10) + missing reason-code → copy mapping (§9.4 contract: "Server-disabled actions use the server-authored structured reason code **and shared frontend copy map**").

## Expected behaviour

`PythonDataService/app/services/operator_capability.py:43-45` says verbatim:

> The reason-code vocabulary is closed and documented; **the Frontend maintains a typed lookup mapping each code to operator-language copy.**

The cockpit was supposed to render operator-language copy for every server-authored reason code (e.g., `BROKER_SAFETY_UNSAFE` → "Broker safety verdict is UNSAFE — non-paper signals detected. Resume is disabled by the server until the verdict returns to paper-only.").

## Observed behaviour

- `cockpit-shell.component.html:231-287` — every action button's `[attr.title]` is `cap.disabled_reason_code ?? 'Resume'`. The raw code (e.g. `BROKER_SAFETY_UNSAFE`, `RECONCILIATION_FAILED`, `MUTATION_UNRESOLVED_STOP`) reaches the operator as the hover tooltip.
- The cockpit also returns the literal string `'TRANSPORT_STALE'` as the title when control-plane transport is degraded — a code the server never authored.
- `audit-tab.component.html:94` — Mark POISONED trigger's title is `mp.disabled_reason_code ?? 'Mark this run poisoned'`. Same raw-code behavior.
- `cockpit-shell.component.html:294-296` — the expanded resume reasons render each code as plain text in the badge.

No file under `Frontend/src/app/` ever mapped these codes to operator language. `grep -rn BROKER_SAFETY_UNSAFE Frontend/src` returns only the test fixtures and the rendering callers; there was no map.

## Why this exists

The closed-vocabulary contract was authored on the server in PRD #616 (and extended in #619-D), and the docstring named the Frontend as the owner of the copy lookup. The Frontend half was not built — the cockpit shipped consuming `disabled_reason_code` as the tooltip directly.

## Fix

Add `Frontend/src/app/components/broker/cockpit-v2/lib/disabled-reason-copy.ts`:

- Strongly-typed `OperatorReasonCode` union (24 codes — covers `REASON_CODES` ∪ `RESUME_REASON_CODES`).
- `LocalReasonCode` union for transient frontend-only codes (`LOCAL_TRANSPORT_STALE`, `LOCAL_REQUEST_IN_FLIGHT`).
- `disabledReasonCopy(code)` resolves to operator-language copy; unknown codes are rendered with the raw code preserved so the regression is visibly diagnosable (run-prompt §9.4).
- `actionTooltip({enabled, serverReasonCode, localTransportStale, busy, fallbackLabel})` composes the action-button tooltip with the priority order documented in the function body (transport-stale > busy > server-reason > fallback).

Add `disabled-reason-copy.spec.ts`:

- Parity test asserting `ALL_OPERATOR_REASON_CODES` equals the expected set drawn from `operator_capability.py` and `resume_guard_state.py`. Adding a new code on the server fails this Vitest immediately.
- Every code maps to copy at least longer than the raw code (catches accidental `copy = code` regressions).
- Composition order assertions for `actionTooltip`.

Wire:

- `cockpit-shell.component.ts` — `actionButtonTooltip(name, fallbackLabel)` method.
- `cockpit-shell.component.html` — replace every `[attr.title]="... ?? '...'"` with `[attr.title]="actionButtonTooltip('resume', 'Resume')"`.
- `audit-tab.component.ts` — `markPoisonedTooltip()` and `markPoisonedDisabledLine()` use the copy map.
- `audit-tab.component.html` — same wiring.

Update the existing cockpit-shell spec that asserted `title === 'BROKER_SAFETY_UNSAFE'` and `title === 'TRANSPORT_STALE'`: they now assert operator-language strings ("UNSAFE", "paper-only"; "transport", "connected").

## Regression coverage

- `disabled-reason-copy.spec.ts` — 12 tests at this finding's first commit, including the closed-vocabulary parity. Later expanded by R-001-F4 (snapshot-load assertion) and the CR-6 prototype-chain regression batch; see the spec's current `describe`/`it` count for the live total.
- `cockpit-shell.component.spec.ts` — updated assertions on resume tooltip and transport-stale tooltip (now operator language).

## Status

Fixed in this PR. The audit-tab Mark POISONED tooltip + disabled-reason line are also rewritten through the same map. No new spec for audit-tab in this pass (no spec existed); the copy-map parity test + cockpit-shell coverage is the regression net.

## Out of scope (for this finding)

`broker-deploy-form.component` and `broker-start-stop-card.component` are not connected to `operator_surface.actions.*` and have their own connectivity-derived disable signals. F-007 covers the legacy-component triage. The shared copy map is available if those surfaces opt in later.
