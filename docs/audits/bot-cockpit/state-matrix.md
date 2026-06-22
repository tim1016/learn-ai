# Capability / state matrix coverage — Bot Cockpit audit 2026-06-22

Source: run-prompt §8.5 ("Capability/state matrix — Build from actual server contracts, not by guessing").

This matrix records the combinations that the **existing test surface** exercises against the cockpit (per-state assertion of the five action capabilities), augmented by the new specs this audit ships. It is not a brute-force cartesian-product enumeration; it is the coverage that the unit + spec suite actually pins.

## Independent axes (from `operator_surface` schema)

- **Intent** (`desired_state.state`): RUNNING · PAUSED · STOPPED · UNKNOWN (when sidecar absent)
- **Host process** (`operator_surface.host_process.state`): RUNNING · STOPPING · EXITED · IDLE · WAITING_FOR_HOST · UNREACHABLE
- **Broker safety verdict** (`operator_surface.broker.safety_verdict`): PAPER_ONLY · UNSAFE · UNKNOWN
- **Broker connection** (`operator_surface.broker.connection`): CONNECTED · DISCONNECTED · UNKNOWN
- **Readiness** (`readiness.verdict`): READY · DEGRADED · BLOCKED · UNKNOWN
- **Runtime freshness** (`runtime_freshness.posture_demoted` + `stale_reason_codes[]`): demoted vs fresh
- **Control plane** (`control_plane.state`): CONNECTED · RETRYING · UNREACHABLE · AUTH_FAILED · PROTOCOL_ERROR · INCOMPATIBLE_CONTRACT
- **Live binding present / absent**
- **Owned positions present / absent**
- **Poisoned / not**
- **Mutation attempt** (`latest_mutation.dispatch_state`): PREPARED · DISPATCHING · RESPONSE_CONFIRMED · OUTCOME_UNKNOWN · EFFECT_CONFIRMED · EFFECT_NOT_OBSERVED · NOT_PROVABLE · EVIDENCE_CONFLICT
- **Broker observation consistency**: CONSISTENT · CONFLICTING · UNKNOWN · NOT_COMPARABLE
- **Request idle / in-flight**

## Coverage assertions

Below: only the **pinned** cells in the spec suite. Where a cell is "pinned by the resolver test", it means `PythonDataService/tests/services/test_operator_capability.py` (existing) exercises it.

### Action enable / disable matrix (`operator_capability.evaluate_all_actions`)

| Action | Pinned states | Resolver test | Cockpit spec |
|---|---|---|---|
| Resume | intent {RUNNING, PAUSED, STOPPED}; safety {paper-only, unsafe, unknown}; reconciliation {passed, failed, stale, not_available, unknown}; uncertain-intent {none, present, unknown}; mutation matrix {none, MUTATION_UNRESOLVED_RESUME, MUTATION_UNRESOLVED_STOP for STOP/RESUME}; posture {fresh, demoted} | test_operator_capability.py | cockpit-shell.component.spec.ts (UNSAFE → tooltip carries operator copy, P1/P2 audit) |
| Pause | intent {RUNNING, PAUSED, STOPPED}; poisoned {yes, no} | test_operator_capability.py | cockpit-shell.component.spec.ts (always shown on identity strip) |
| Stop | intent {RUNNING, PAUSED, STOPPED}; poisoned {yes, no} | test_operator_capability.py | cockpit-shell.component.spec.ts (single canonical render site test) |
| Flatten and pause | live_binding {present, absent}; owned_positions {present, empty} | test_operator_capability.py | cockpit-shell.component.spec.ts (action button present + dispatched) |
| Mark poisoned | live_binding {present, absent}; poisoned {yes, no} | test_operator_capability.py | audit-tab — tooltip via copy map (P2-002) |

### Independent-indicator rendering (ADR-0013 §3 Playwright meta-rule, applied via Vitest)

| Indicator | Cockpit spec |
|---|---|
| `indicator-process` | "renders five independent indicators on the identity strip" |
| `indicator-intent` | as above |
| `indicator-readiness` | as above |
| `indicator-broker` | as above |
| `indicator-safety` | as above |
| Env chip (page-utility row) | "env chip renders %s verdict as label %s (not synthesized from status truthiness)" — three params: PAPER_ONLY → PAPER · UNSAFE → UNSAFE · UNKNOWN → UNKNOWN |

### Control-plane × transport-stale × action dispatch (PRD #619-C4)

| Cell | Cockpit spec |
|---|---|
| CONNECTED → no banner, dispatches allowed | "hides the control-plane banner when the state is CONNECTED" |
| RETRYING → ATTENTION banner with attempt count | "renders a RETRYING banner with attempt count and server-authored notice" |
| UNREACHABLE / AUTH_FAILED / PROTOCOL_ERROR / INCOMPATIBLE_CONTRACT → LAST-KNOWN demoted banner | "renders a LAST-KNOWN demoted banner for %s" parametrized over the four states |
| Demoted → action buttons refuse local dispatch with operator-language copy | "transport-stale tooltip is operator-language, not raw 'TRANSPORT_STALE' code" (updated by P2-002) |
| Demoted → `dispatchPause` no-ops with mutation error | "dispatchPause refuses to fire when transport is stale and surfaces a mutation error" |

### Runtime freshness × stale reason codes (PRD #619-B7)

| Cell | Cockpit spec |
|---|---|
| `posture_demoted` with `stale_reason_codes` | "renders runtime demotion reason codes prominently" — banner contains LAST-KNOWN + the codes verbatim |

### Disabled-reason copy map (P2-002 parity)

| Cell | Spec |
|---|---|
| Every code in `REASON_CODES ∪ RESUME_REASON_CODES` mapped | disabled-reason-copy.spec.ts "covers every code in the expected vocabulary, and no extras" |
| No raw code reaches the operator tooltip | disabled-reason-copy.spec.ts "every operator code maps to non-trivial operator-language copy" (longer than the raw code) |
| Unknown server code preserved verbatim | disabled-reason-copy.spec.ts "returns a visibly diagnosable string for an unknown code (run-prompt §9.4)" |
| Composition priority: transport-stale > busy > server > fallback | disabled-reason-copy.spec.ts four composition tests |

## Not pinned in this audit

- Playwright cockpit e2e specs against a live container stack — out of scope for an interactive-mode run; the existing `Frontend/tests/e2e/cockpit-*.spec.ts` (ADR-0013 §3 meta-rule consumers) are unchanged by this audit. The Vitest assertions cover the per-component contract.
- Full enumeration of the 11-axis cartesian product. Cells not pinned above are covered by the resolver's pure tests on the Python side (one assertion per axis combination that matters).
