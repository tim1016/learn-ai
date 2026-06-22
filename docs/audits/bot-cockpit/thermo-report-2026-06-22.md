# Thermo-nuclear code-quality review — Bot Cockpit audit 2026-06-22

Branch: `auto/bot-cockpit-remediation-2026-06-22` vs `master` (`77bf6563`).

The user explicitly chose "I run thermo per the prompt" at session start (memory entry `feedback_pr_workflow.md` says they prefer to invoke thermo themselves, but for this single run the prompt's §3.4 gate 4 wins per their answer). Running `Skill(thermo-nuclear-code-quality-review)` and applying the rules below directly to the diff.

## Diff shape

```
Net change: +1660 / −703 = +957 lines, but −656 of which is legacy code deletion.
Functional net code change: ~+345 lines, focused.

Code:
- New module: cockpit-v2/lib/disabled-reason-copy.ts (176 lines)
- New spec:  cockpit-v2/lib/disabled-reason-copy.spec.ts (167 lines)
- Touched: cockpit-shell.component.ts (+56) / .html (+44 / −22) / .spec.ts (+72)
- Touched: tabs/audit-tab.component.ts (+25) / .html (+6 / −3)
- Touched: reused/host-process-notice.component.ts (+6 doc-comment only)
- Deleted: broker-start-stop-card/{html,scss,spec.ts,ts} (4 files, 656 lines)

Docs / audit artifacts (not code):
- New: docs/audits/bot-cockpit/charter.md, decisions-for-review.md,
        baseline-2026-06-22.json, affordance-inventory.md, state-matrix.md,
        findings/P{1,2,3}-*.md, this report
- New: docs/audits/bot-cockpit/run-{prompt,sh,log} + launchd-{stdout,stderr}.log
  (the failed-overnight evidence)
- Touched: docs/operator-architecture-and-runbook.md, docs/runbooks/broker-instance-operator-surface.md
```

## Rule-by-rule findings

### Rule 0 — Code-judo / dramatic simplification

**Search.** Did I miss a structural simplification?

- Could the disabled-reason-copy map have been inlined per call-site? No — it would duplicate the 24-entry table across two components and lose the parity-test single point. The shared module is the right abstraction.
- Could `actionButtonTooltip(name, fallbackLabel)` be inlined into the HTML? No — Angular templates can't call functions imported from a module without going through a method. The wrapper makes the call site testable from the template.
- Could the env chip be a `data-value` attribute on the existing identity-strip SAFETY indicator instead of a separate chip? In principle yes — and that would delete the chip entirely. But the chip is in the **page utility row** (the title bar at the very top of the cockpit, distinct from the identity strip lower down), and pulling it would change the visual contract for an operator who reads "PAPER" before they read anything else. Closing the regression by binding to the same server field is the minimal correct fix; deleting the chip is a UX decision out of audit scope.

**Verdict:** No missed code-judo move.

### Rule 1 — File size

| File | Before | After | Threshold |
|---|---|---|---|
| `cockpit-shell.component.ts` | 615 | 671 | < 1000 ✓ |
| `cockpit-shell.component.html` | 420 | 422 | < 1000 ✓ |
| `cockpit-shell.component.spec.ts` | 470 | 505 | < 1000 ✓ |
| `disabled-reason-copy.ts` | (new) | 176 | < 1000 ✓ |
| `disabled-reason-copy.spec.ts` | (new) | 167 | < 1000 ✓ |
| `audit-tab.component.ts` | 54 | 79 | < 1000 ✓ |

**Verdict:** No file crosses the 1000-line threshold. The cockpit-shell grew by ~9% — proportionate to the two structural fixes (envChip + actionButtonTooltip helper).

### Rule 2 — Spaghetti growth in existing code

- The new `envChip` computed is a closed-enum switch with three exhaustive cases. No special-case branches bolted into the existing `controlPlaneBanner` / `localTransportStale` flows; new state, new computed.
- `actionButtonTooltip(name, fallbackLabel)` is a single typed delegation to the shared `actionTooltip` composition function. No new conditionals in the rest of the component.
- The HTML diff replaced **four identical** ternary-conditional `[attr.title]` expressions with **four identical** function calls. That's not spaghetti — it's removing it.

**Verdict:** No spaghetti growth.

### Rule 3 — Cleaning the design vs. accepting "working code"

- Pre-audit, the cockpit shipped a 5-token expression `localTransportStale() ? 'TRANSPORT_STALE' : (resume.disabled_reason_code ?? 'Resume')` in HTML, four times, untested. The audit replaces it with a typed function call routed through a parity-tested map. The design is now cleaner, not "working code that's the same shape."
- The env chip was a one-token `status() ? 'PAPER' : ''` that violated ADR-0013. It is now a typed `envChip()` computed that consumes the server-authored enum. Cleaner.

**Verdict:** Design improved at every touched call site.

### Rule 4 — Boring / maintainable over hacky

- The new `disabled-reason-copy.ts` is a plain `Record<UnionType, string>` lookup + a four-case priority function. No magic.
- `ALL_OPERATOR_REASON_CODES` is `Object.keys(OPERATOR_REASON_COPY) as OperatorReasonCode[]` — a single cast where the runtime invariant (every key in the record is a member of the union) is guaranteed by TS's `Record<>` type. The cast is the smallest necessary.

**Verdict:** Direct, boring, maintainable.

### Rule 5 — Type / boundary cleanliness

- `OperatorReasonCode` is a closed union of 24 string literals. No `any`. No `unknown` (the unknown-code path takes `string` deliberately and returns operator-readable copy preserving the raw token).
- The Vitest parity test pins the union against a hardcoded `EXPECTED_OPERATOR_REASON_CODES` set; the test fails on drift (missing OR extra), so the union can't silently fall out of sync with the Python source-of-truth.

**Verdict:** Types and boundaries explicit; no cast/optionality churn introduced.

### Rule 6 — Canonical layer / reuse

- The new module sits next to existing cockpit-v2/lib utilities (`account-summary-attention.ts`, `classify-readiness-transition.ts`, `clock-sync.ts`, `instance-tab-state.ts`, `suggested-action-renderer.ts`). Same kind of thing in the same place.
- No bespoke duplication of the operator-capability evaluator; the cockpit consults the server's `disabled_reason_code` and renders, never deriving.

**Verdict:** Logic in the right layer; reuses the lib/ neighborhood.

### Rule 7 — Sequential orchestration / atomicity

- No new orchestration code. The fix is a pure value transformation (verdict → chip, code → copy). No async flow changes.

**Verdict:** N/A.

## Approval-bar gates

- [x] No clear structural regression — net change is more focused, not less.
- [x] No obvious missed code-judo move (above analysis exhausted plausible candidates).
- [x] No file-size explosion past 1000 lines.
- [x] No new spaghetti / special-case branches.
- [x] No hacky / magical abstraction.
- [x] No unnecessary wrapper / cast churn — every wrapper is justified by a testability or template constraint.
- [x] No architecture-boundary leak — operator-language copy lives Frontend-side per ADR-0013 §4; resolver eligibility stays server-side.
- [x] No avoidable canonical-helper duplication — the parity test enforces single-source-of-truth for the closed vocabulary.
- [x] No missed decomposition — the new module is decomposed by topic (server reason codes / local reason codes / composition); not over-decomposed.

## Verdict

**PASS.** No major findings. No blockers. The branch is ready for project-scope lint + test confirmation and push.

### Minor (deferred — not blockers)

- The `envChip` computed switch could carry an exhaustiveness check (e.g. `default: const _exhaustive: never = verdict; return _exhaustive;`) to make adding a fourth verdict trigger a TS error at compile time instead of a silent missing case. Today the union is closed (3 values) and the test parity-locks it, so the runtime risk is zero. Optional polish.
- `cockpit-shell.component.ts` is 671 lines and growing organically. Not yet a problem, but worth flagging for the next audit pass.

## Major findings re-pushes will trigger thermo? No — per CLAUDE.md, the thermo gate fires only on the first PR push. Re-pushes after PR review do NOT re-trigger thermo. The gate is one-shot.
