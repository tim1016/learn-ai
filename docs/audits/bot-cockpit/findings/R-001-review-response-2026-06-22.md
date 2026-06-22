# R-001 — PR #642 code-review response (2026-06-22)

The user ran code-review against the initial audit branch and surfaced seven items. Each is addressed below, with the commit it landed on.

## R-001-F1 — Tooltips pointed at a Reconcile control that does not exist (P1)

**Reviewer.** `disabled-reason-copy.ts` repeatedly says "Use Reconcile on the Audit tab," but the cockpit has no mutation-reconcile control or frontend client for `/reconcile-mutation`. It simultaneously displays `RECONCILE · NOT WIRED`.

**Verified.** `grep -rn 'reconcile-mutation\|reconcileMutation' Frontend/src/app/services/ Frontend/src/app/api/` returned zero hits. The closest thing to a Reconcile UI is the `RECONCILE · NOT WIRED` hazard banner around `cockpit-shell.component.html:301-311`.

**Fix.** `disabled-reason-copy.ts` MUTATION_UNRESOLVED_* and OUTCOME_UNKNOWN copy now reads `Reconcile via POST /api/live-instances/{id}/reconcile-mutation before retrying (see broker-instance-operator-surface runbook).` `docs/runbooks/broker-instance-operator-surface.md` gains a "Reconcile procedure (no cockpit button yet)" section that documents the curl + jq workflow and names the button-wiring as deferred work. Wiring the Reconcile button into the cockpit UI is a follow-up out of this audit's scope.

## R-001-F2 — Transport-stale gate disabled Pause and Stop (P1, ADR-0004 D violation)

**Reviewer.** `cockpit-shell.component.html` applied `localTransportStale()` to every action, conflicting with the ADR-0004 amendment D contract that durable Pause / Stop must remain available during control-plane / runtime demotion.

**Verified.** Pre-fix HTML had `[disabled]="!X.enabled || busyAction() !== null || localTransportStale()"` on all four buttons. ADR-0004 amendment D §"Action policy is asymmetric by safety effect":

> Resume and Flatten-and-pause disable while posture is demoted because they require current runtime evidence.
> Durable Pause and Stop remain available because removing the operator's fail-safe intent controls during a control-plane/runtime incident would be less safe.
> Mark-poisoned remains available as an incident-recovery action.

**Fix.** New `isLocalTransportGatedFor(name)` predicate returns `true` only for `resume` / `flatten_and_pause`. The HTML disabled bindings for Pause / Stop / Mark-Poisoned no longer include `localTransportStale()`. The dispatch methods `dispatchPause` / `dispatchStop` / `openTypedHalt` removed their `_refuseOnStaleTransport` short-circuits. `actionButtonTooltip` only applies the LOCAL_TRANSPORT_STALE composition rule for the two gated actions. Two new component specs lock the behaviour: "leaves Pause + Stop enabled when control_plane is RETRYING (ADR-0004 D fail-safe)" and "dispatchPause fires through transport-stale (durable Pause is fail-safe)." Two pre-existing specs that locked the wrong behaviour were rewritten.

## R-001-F3 — Disabled tooltips were not keyboard-accessible (P2)

**Reviewer.** Native `title` attributes on disabled buttons are inconsistent across browsers and disabled buttons can't receive keyboard focus.

**Fix.** Each action button now carries `aria-describedby` pointing at a sibling focusable `<p class="action-help">` with the same operator-language text, rendered via `aria-live="polite"`. Screen-reader users get the explanation via the description relationship; sighted keyboard users see the visible help text. The disabled button still has `title` for hover; the surrogate paragraph handles the keyboard / a11y surface.

## R-001-F4 — The "server parity" test compared two TS lists (P2)

**Reviewer.** The original `EXPECTED_OPERATOR_REASON_CODES` was a hand-maintained TypeScript array, not the live Python set. A new Python code would not fail this test.

**Fix.** Replaced with a true cross-stack parity:

1. `PythonDataService/scripts/regenerate_operator_reason_codes_snapshot.py` reads the live `REASON_CODES` frozenset and writes two byte-identical JSON snapshot files (one in `PythonDataService/app/services/`, one in `Frontend/src/app/components/broker/cockpit-v2/lib/`).
2. `PythonDataService/tests/services/test_operator_reason_codes_snapshot.py` asserts the Python-tree snapshot matches the live `REASON_CODES` set.
3. `Frontend/src/.../disabled-reason-copy.spec.ts` imports the Frontend-tree snapshot via `resolveJsonModule` and asserts it matches `ALL_OPERATOR_REASON_CODES`.

A new Python code without a snapshot regen fails (1). A snapshot edit without a Frontend map update fails (3). A snapshot edit without matching Python source fails (2). The two-file design is required because the two test containers do not share a working tree — each side anchors to its own committed copy of the same snapshot.

## R-001-F5 — Multi-reason expansion still rendered raw codes (P2)

**Reviewer.** `cockpit-shell.component.html` lines 294-299 printed `disabled_reasons` verbatim.

**Fix.** Updated the `@for` to render `disabledReasonCopy(code)` instead of the raw code. The raw code is preserved on `data-reason-code` and `[attr.title]` for diagnostics and Playwright. Wired a `disabledReasonCopy()` method on the component for template access.

## R-001-F6 — Two Playwright assertions expected raw codes (CI blocker)

**Reviewer.** `cockpit-actions.spec.ts` had two assertions that the disabled-button `title` attribute equals `BROKER_SAFETY_UNSAFE` and `NO_OWNED_POSITIONS`. With P2-002 these now show operator copy; the assertions fail.

**Fix.** Both assertions rewritten to expect operator-language behaviour (`title` does NOT equal the raw code; `title` contains specific operator words like "UNSAFE", "paper-only", "flatten", "positions"). The raw code is moved to `data-disabled-reason-code` on the button for diagnostics; the Playwright tests now also assert the data attribute carries the closed enum.

## R-001-F7 — Two §9.6 in operator runbook (doc defect)

**Reviewer.** `docs/operator-architecture-and-runbook.md` had two sections numbered 9.6.

**Fix.** The halt-flag section is now §9.7.

---

## Coverage results after fix

- Pytest: `test_operator_reason_codes_snapshot.py` — 7/7 pass.
- Vitest: `disabled-reason-copy.spec.ts` — 13/13 pass (was 12; +1 for the snapshot-load assertion). `cockpit-shell.component.spec.ts` — 24/24 pass (was 23; +1 for the ADR-0004 D fail-safe assertion). Full `cockpit-v2/**` Vitest — 131/131 pass.
- Playwright: assertions updated to match the operator-language tooltips and the new `data-disabled-reason-code` attribute. Suite is in-tree; CI exercises it.
- Operator runbook: two §9.6 collapsed to §9.6 + §9.7. The broker-instance-operator-surface runbook now documents the Reconcile procedure (no cockpit button yet) so the tooltip's deep-link target exists.
