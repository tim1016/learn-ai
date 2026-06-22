# P1-001 — The "PAPER" environment chip is driven by `status()` truthiness, not by the broker safety verdict

## Where

`Frontend/src/app/components/broker/cockpit-v2/cockpit-shell.component.html:6`

```html
<span class="env">{{ status() ? 'PAPER' : '' }}</span>
```

## Severity

**P1** — material state dishonesty (run-prompt §10). The cockpit displays the string `PAPER` whenever a status response has been received, regardless of the actual `operator_surface.broker.safety_verdict`. If the broker safety verdict is `unsafe` or `unknown`, the chip still shows `PAPER`.

This is not a hypothetical: ADR-0011 §1 (reactive `BrokerSafetyVerdict`) and ADR-0013 §1 (the verbatim rule) require that the cockpit consume the server-authored safety verdict and not derive it from any other signal. The truthiness of an HTTP response payload is not the safety signal.

## Expected behaviour

Per ADR-0013 §1 ("verbatim rule") and ADR-0011 (reactive halt on verdict transition), the environment chip must reflect the server-authored `broker.safety_verdict`:

- `paper-only` → `PAPER`
- `unsafe` → `UNSAFE`
- `unknown` → `UNKNOWN`

The `SAFETY` indicator in the identity strip already does this correctly (`cockpit-shell.component.html:208–213`). The page-utility-row chip duplicates the visual claim *without* the verdict gate, creating an internally inconsistent UI where the chip and the indicator can disagree.

## Observed behaviour

`status()` is a signal set whenever any `/api/live-instances/{id}/status` payload returns. There is no path where this signal is truthy and the broker safety verdict is verified — so the chip says `PAPER` immediately on first paint, before the broker safety verdict is even rendered.

In the failure mode (broker safety verdict = `unsafe`), the chip says `PAPER` and the `SAFETY · unsafe` indicator says the opposite. A reasonable operator reads the chip first (it's nearer the title, larger contrast in the header) and the indicator second; ADR-0011 designed the indicator to be load-bearing, but the chip undercuts it.

## Why this exists

The chip was added to make the cockpit title read "Terminal Cockpit · PAPER" so operators know they are not on the live cockpit. The shortcut "if status is loaded, we're in PAPER mode" was true at the moment the chip was authored — there was no live mode yet. The shortcut is no longer true and was never reviewed against ADR-0013.

## Fix

Drive the chip from `operator_surface.broker.safety_verdict` and render the closed verdict vocabulary (`paper-only` / `unsafe` / `unknown`) verbatim. Hide the chip when `status()` is null (no claim is also honest). Add a Vitest regression that proves the chip text equals the verdict for each verdict value.

## Regression test

Vitest in `cockpit-shell.component.spec.ts` — three cases (`paper-only`, `unsafe`, `unknown`) each driven by a fixture status and asserted via `screen.getByTestId('env-chip')`.

## Status

Fix in this PR. Test in this PR. Doc reconciliation: `docs/operator-architecture-and-runbook.md` already documents the indicator strip — this fix brings the title-row chip into the same authority.
