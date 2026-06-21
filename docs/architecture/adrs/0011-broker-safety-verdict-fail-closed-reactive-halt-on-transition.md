# ADR 0011 — Broker safety verdict: per-gate, fail-closed, reactive, order-blocking on unsafe, start-blocking on unknown, halt-on-transition on degradation, guarded Resume

**Status:** Proposed 2026-06-14. **Amended 2026-06-21 (PRD #619-A) — Decision §2 derivation: `paper-only` no longer requires `readonly_flag == true`. Identity (`configured_mode=paper ∧ paper_port ∧ DU account`) and submission capability are independent facts. See "Amendment 2026-06-21" block after Decision §9.** Vocabulary recorded in `CONTEXT.md` § "Broker safety verdict" and § "QC provenance card split". Grilling session: `grill-with-docs` 2026-06-14 against `docs/audits/vibe-coded-app-remediation-prd.md`. Load-bearing code claims (four-layer paper enforcement in `broker/ibkr/orders.py::place_paper_order`; hardcoded "Paper trading mode" string in `broker-instances` hero per VCR-0010; `qc_cloud_backtest_id` labelled "QC-approved" per VCR-0014) verified before the session.
**Decision drivers:** VCR-0010 found the `broker-instances` cockpit hero displays the static string *"Paper trading mode — no real money at risk"* with no reactive consultation of the actual broker mode. The runtime enforces paper-only at four layers — `IBKR_READONLY`, `IBKR_MODE=paper`, port-not-in-`LIVE_PORTS`, `account_id.startswith("DU")` — but the **hero is a trust anchor**: an operator reading it on a misconfigured deploy (one or more enforcement layers in an unexpected state) would receive false reassurance even though the runtime is blocking. The flip case — a future toggle to live mode — would not be reflected in the hero. VCR-0014 found `qc_cloud_backtest_id` labelled "QC-approved" with no verification path; the operator-recorded id is treated as if it were a verified fact. Both are instances of the same UI-truth failure: a label promising a guarantee independent of any verifiable runtime fact. The grilling session generalized this into a verdict design that (a) binds the hero to a server-derived structured judgment, (b) treats the verdict as a runtime gate not just a display, and (c) splits the provenance card so verified facts and operator-recorded facts cannot share a single misleading label.
**Related:** ADR 0002 (shadow-mode adapter-level no-submit — a sibling enforcement layer), ADR 0006 (deploy / account_id hashed into `run_id` — the deploy-time identity that ADR 0011's connected-account-prefix gate cross-checks at runtime), ADR 0008 (durable submit / order identity — `SUBMIT_UNCERTAIN_HALT` is a sibling halt path with similar Resume-guard semantics), ADR 0010 (operator-action contract — Resume's guarded-write contract composes with this ADR's verdict gate), ADR 0009 (live sizing authority — the audit-copy allow-list this ADR's QC provenance card surfaces), `CONTEXT.md` § "Broker safety verdict", `CONTEXT.md` § "QC provenance card split", `CONTEXT.md` § "Readiness gate", `docs/audits/vibe-coded-app-remediation-prd.md` Phase 7, `.claude/rules/numerical-rigor.md` ("numerical claims require receipts").

## Context

Today's runtime + UI state (verified against the code at session time):

| Concern | Today |
|---|---|
| Paper-only enforcement | `broker/ibkr/orders.py::place_paper_order` enforces paper-only at four layers before any order is placed: `IBKR_READONLY` flag, `IBKR_MODE=paper`, port-not-in-`LIVE_PORTS`, `account_id.startswith("DU")`. Connection-time, the IBKR client also fails closed if `managedAccounts()` returns more than one account (no silent FA-sub-account selection). |
| Cockpit hero | `broker-instances` hero renders a hardcoded string "Paper trading mode — no real money at risk" with no consultation of the runtime gates. |
| QC provenance card | `qc_cloud_backtest_id` is rendered with a label that implies verification ("QC-approved" or equivalent) although no QC Cloud API verification path exists. |
| Readiness model | CONTEXT.md § "Readiness gate" already established the *engine-authored* readiness verdict (instance-scoped, structured `{verdict, summary, gates: […]}`). The broker safety surface fits the same model but at a different altitude: it is about the **enforcement environment**, not the strategy's bar-loop readiness. |
| Account identity gate | Phase 3 of the remediation PRD (`ledger.account_id == broker.connected_account` strict match, halt on reconnect mismatch) is a sibling runtime gate. It overlaps with this ADR's `connected_account_prefix` gate but checks a different thing (identity match, not prefix shape). Both are runtime-enforced. |

The audit grilling enumerated four design axes:

1. **Granularity.** Verdict-only string vs. per-gate breakdown plus derived verdict. Verdict-only forces operators to file a support ticket to find out why the hero is amber; per-gate breakdown makes the failing input testable independently.
2. **Derivation rule.** Optimistic ("assume paper unless we see live"), pessimistic ("require positive paper confirmation on every gate"), or hybrid. The hero is a *trust anchor*; trust nothing it cannot verify → fail-closed.
3. **Reactivity.** On-connect snapshot vs. reactive on the existing broker-status transport. A connect-time snapshot can mask a mid-session env-reload, gateway reclassification, or reconnect to a different account; reactive ties the verdict to the same payload that already updates the cockpit.
4. **Runtime force.** Hero color only vs. order-blocking on unsafe + start-blocking on unknown + halt-on-transition on degradation. Hero-only leaves the runtime continuing to act under a verdict that says it should not — the failure mode VCR-0010 framed as "the day a 'live mode' path lands, this hero becomes silent corruption of operator expectations." Runtime force makes the verdict load-bearing.

## Decision

### 1. Verdict shape — per-gate + derived `final_verdict`

```ts
type BrokerSafetyVerdict = {
  configured_mode: "paper" | "live" | "unknown";
  readonly_flag: boolean | null;
  port_class: "paper_port" | "live_port" | "unknown";
  connected_account_prefix: "DU" | "non_DU" | null;
  final_verdict: "paper-only" | "unsafe" | "unknown";
  failing_gates: string[];   // gates that positively indicate live/non-paper
  unknown_gates: string[];   // gates whose state cannot be confirmed
};
```

Per-gate fields are independently testable (every gate's derivation is one assertion in a parameterized test). `failing_gates` and `unknown_gates` carry the labels the cockpit and CLI surface; the operator never needs to compose a reason from raw fields.

### 2. Fail-closed derivation — `paper-only` requires positive confirmation on every gate

```
final_verdict =
  "paper-only" iff every required gate positively confirms paper:
    configured_mode == "paper"
    AND readonly_flag == true
    AND port_class == "paper_port"
    AND connected_account_prefix == "DU"

  else "unsafe" iff any gate positively indicates live/non-paper risk:
    configured_mode == "live"
    OR port_class == "live_port"
    OR connected_account_prefix == "non_DU"

  else "unknown"
```

The hero is a trust anchor; any missing signal degrades to `unknown`, never to `paper-only`. The `unsafe` verdict is reserved for a *positive* indication of live risk — not for an absence of paper confirmation. This three-way split (`paper-only` / `unsafe` / `unknown`) corresponds to three different operator actions: keep going, block immediately, investigate.

### 3. Reactive transport — rides the existing broker status / readiness payload

The verdict is computed in the Backend and embedded in the existing broker-status / readiness payload that the cockpit polls (or receives via SSE). No new transport. No connect-time cache as the source of truth.

Rationale: a connect-time snapshot would mask a mid-session env-reload, gateway reclassification, account-disconnect-and-reconnect-to-different-account, or `IBKR_READONLY` toggle. The verdict must be re-derived every poll cycle so any state change is visible on the next render.

### 4. Runtime force — verdict gates submission, start, AND continuation

The verdict is not a UI hint. It is a runtime gate.

- **`unsafe` is order-blocking.** Engine refuses to submit any new order while `final_verdict == "unsafe"`. The four-layer enforcement in `place_paper_order` continues to exist as defense in depth; the verdict gate is the higher-altitude refusal that fires before sizing resolution and intent_id mint.
- **`unknown` is start-blocking.** `cmd_start` refuses to bring up a new run when `final_verdict == "unknown"`, except via a separately-named, explicitly documented diagnostic / read-only path. The diagnostic path is **not** an accidental fall-through — it must be a distinct code surface (CLI flag, separate endpoint), and it MUST NOT place orders.
- **Halt-on-transition.** A run that started under `paper-only` and observes `final_verdict != "paper-only"` mid-session fatally halts. See Decision 5.

### 5. Halt-on-transition — degrading mid-session is a fatal halt to `desired_state=PAUSED`

A running bot captures `startup_broker_safety_verdict` at run start. A `verdict_transition_observer` in the bar loop / status-update path compares each new verdict against the startup verdict. Any transition out of `paper-only`:

1. Block new order submission immediately.
2. Write `halt.flag` (existing fatal-halt artifact).
3. Set durable `desired_state = PAUSED` — **not** `STOPPED`. Verdict transitions may be transient (broker disconnect, gateway restart, probe failure). `PAUSED` is reversible after operator inspection per ADR 0010. `STOPPED` is instance retirement, which is the wrong shape for a transient safety-signal degradation.
4. Emit `BROKER_SAFETY_VERDICT_TRANSITION_HALT` WAL event carrying the old verdict, new verdict, full per-gate snapshot, and `failing_gates` / `unknown_gates` lists. The cockpit failure list renders this event with the offending gate.
5. Stop / suspend the active trading loop per the existing fatal-halt mechanics.

### 6. Resume is guarded — never bypasses the verdict

Per ADR 0010's guarded-write contract for Resume: the cockpit's Resume action is mechanically a write of `desired_state = RUNNING`, but the endpoint consults the current verdict before promoting state. If `final_verdict != "paper-only"`, durable state stays `PAUSED` and the API surfaces `broker_safety_not_paper_only`. The verdict gate is read-only from the operator's perspective — the button cannot bypass it.

This means a verdict transition halt's recovery loop is structural: operator inspects the failing gate, fixes the configuration (or waits for the broker to reconnect to the right account), the next status poll re-derives the verdict, the cockpit shows `paper-only` again, and Resume succeeds.

### 7. Frontend renders the server-derived verdict — never composes its own

The Frontend MUST NOT independently derive `final_verdict` from the per-gate fields. It renders `final_verdict` directly. Per-gate fields are surfaced as a breakdown (expandable list, tooltip) so the operator can see *which gate failed* without filing a support ticket — but the verdict itself is server-authored.

Rationale: the same single-source-of-truth principle that CONTEXT.md applies to readiness applies here. If two clients derive the verdict, they will eventually disagree.

### 8. Feeds the start-readiness gate

A start-readiness verdict (CONTEXT.md § "Readiness gate") of `READY` requires `final_verdict == "paper-only"` as a hard input. A `BLOCKED` verdict surfaces the failing gate as a `hard` readiness gate input. This makes the broker safety verdict observable from the readiness shape that the cockpit and CLI already render — no second display path.

### 9. QC provenance card split — verified facts and operator-recorded facts cannot share a label

The same single-claim-multiple-truths failure that VCR-0010 found in the hero, VCR-0014 found in the provenance card. The card is split:

```ts
type QcProvenance = {
  audit_copy_path: string;
  audit_copy_sha256: string;
  audit_copy_sha256_verified: boolean;
  audit_copy_sizing_rule_verdict?: "proven_match" | "proven_mismatch" | "cannot_prove";
  qc_cloud_backtest_id: string;
  qc_cloud_backtest_id_verified: false;  // always false until a real QC Cloud API verification path exists
};
```

Two rows:

- **Audit copy:** ✓ SHA verified against on-disk file + ADR 0009 audit-copy sizing allow-list — OR — ✗ SHA not verified / cannot prove (with `audit_copy_sizing_rule_verdict` rendered if available).
- **QC Cloud backtest:** `{id}` — *Operator-recorded, not auto-verified.*

The following labels are **forbidden** in code and copy until a real QC Cloud API verification path exists: `"QC-approved"`, `"Byte-identical to backtest"`, `"verified backtest"`. The fail-closed framing from the broker safety verdict applies here too: an unverified claim must not be labelled as if it were verified.

## Amendment 2026-06-21 (PRD #619-A) — identity vs. submission capability are independent facts

**Driver.** The original Decision §2 required `readonly_flag == true` for `paper-only`. `IBKR_READONLY=true` blocks order placement at the lowest layer; an executing paper bot must run with `readonly=false`. Under the original derivation, an order-capable paper run can never obtain `paper-only`, but guarded Resume in Decision §6 requires `paper-only`. The two contracts disagree. The audit triggering PRD #619 found `live_instances._resolve_safety_verdict_final` reading non-existent attributes (`client.config.port`, `client.config.read_only_api`) and silently degrading to `unknown` — the regression went unobserved because the identity gate could never be true for a real paper run.

**Decision.** The verdict is split into two independent backend-authored facts. The cockpit and the runtime consult both; the original `BrokerSafetyVerdict` shape carries identity. Submission capability is carried separately at the run/spec level.

**Identity** — what the verdict resolver decides today:

```
broker_identity =
  "paper-only"  iff   configured_mode == "paper"
                  AND port_class == "paper_port"
                  AND connected_account_prefix == "DU"

              else "unsafe"
                  iff   configured_mode == "live"
                     OR port_class == "live_port"
                     OR connected_account_prefix == "non_DU"

              else "unknown"
```

`readonly_flag` is **not** in the identity derivation. The field stays on the `BrokerSafetyVerdict` shape (per-gate breakdown is still useful as diagnostic display), but it no longer contributes to `failing_gates` or `unknown_gates`, and `readonly_flag=False` never blocks `paper-only`.

**Submission capability** — independent fact derived from durable child/run evidence (PRD #619-A §A3):

```
submission_capability =
  "PAPER_ORDERS_ENABLED" iff declared submit_mode in the spec/ledger == "live_paper"
                            AND the child's actual readonly setting at construction == False
  "READ_ONLY"            iff the child's actual readonly setting == True
  "BLOCKED"              iff lower-layer guards (place_paper_order four-layer)
                            positively refuse — e.g., a live port + DU prefix mismatch
  "UNKNOWN"              iff either declared submit_mode or actual readonly cannot
                            be proven from durable child/run evidence
```

Capability authority is **durable child/run evidence only** — not a pre-deploy observation of the data-plane singleton. The pre-deploy singleton snapshot is advisory; it never authorizes Resume.

**Effective posture** — composition consumed by the operator surface:

```
effective_posture =
  "PAPER_EXECUTION"   iff broker_identity == "paper-only"
                          AND submission_capability == "PAPER_ORDERS_ENABLED"
  "PAPER_OBSERVATION" iff broker_identity == "paper-only"
                          AND submission_capability == "READ_ONLY"
  "UNSAFE"            iff broker_identity == "unsafe"
                          OR submission_capability == "BLOCKED"
  "UNKNOWN"           otherwise
```

**Composition rules unchanged:**

- The lower-altitude four-layer `place_paper_order` enforcement is unchanged (defense in depth).
- The connection-time multi-account refusal is unchanged.
- Halt-on-transition (Decision §5) still fires on any `broker_identity` change out of `paper-only` — the trigger is identity-only.
- Guarded Resume (Decision §6) now composes four gates: `broker_identity == "paper-only"` AND `submission_capability` satisfies the declared run `submit_mode` AND `reconciliation in {PASSED, NOT_AVAILABLE}` AND `uncertain_intent is CLEAR`. Per-gate availability semantics are gate-specific and backend-authored — there is no universal "NOT_AVAILABLE never blocks" rule.

**Wire shape preserved.** `BrokerSafetyVerdict.final_verdict` continues to carry `paper-only | unsafe | unknown` and continues to mean identity. `submission_capability` and `effective_posture` are carried on the operator surface DTOs separately (see PRD #619-A and the broker-runtime snapshot path). The verdict_snapshot.json file shape is unchanged in PRD #619-A; its identity field remains the carrier for the identity-only resume gate.

**Why this is the right amendment to ADR 0011 rather than a new ADR.** The original derivation conflated identity and capability into one fact; the runtime contracts that consume the verdict (start-blocking on `unknown`, halt-on-transition on degradation, guarded Resume) all reasoned about identity — the readonly clause was the implementation defect, not the design intent. Splitting capability out preserves every other clause of the ADR verbatim and makes the runtime contracts actually achievable.

## Consequences

**Positive:**

- The hero stops being a trust-anchor lie. An operator who reads "Paper trading mode" can rely on the four gates having positively confirmed paper; an amber hero names which gate failed; a red hero blocks orders before the operator can act on the wrong information.
- The verdict's runtime force closes the failure mode VCR-0010 framed as "the day a live mode path lands, this hero becomes silent corruption of operator expectations." The verdict's halt-on-transition contract means the day a live mode path lands and the verdict changes mid-session, the bot fatally halts — not silently continues to act under a label that no longer applies.
- Per-gate fields are independently testable. The derivation rule is one parameterized test; every gate's source is one independent unit test.
- The QC provenance card stops claiming verification it cannot prove. Operators see exactly which row is a verified fact and which is operator-recorded — making the audit-copy SHA path (which *is* verifiable) the carrier of every "QC anchor" claim, and the QC Cloud backtest id a useful-but-untrusted reference.
- The Resume guard composes with ADR 0010's contract — there is no "operator clicks Resume during an unsafe verdict and the bot trades" path.

**Negative:**

- The Backend gains a verdict resolver wired into the broker-status / readiness payload. Implementation cost is modest (the gates already exist; deriving the verdict is mechanical) but it adds a Backend responsibility that did not exist before.
- The cockpit gains a per-gate breakdown surface (tooltip or expandable list) — UI work that did not exist before.
- Operators trained on the current "Paper trading mode" hero must learn that the amber / red verdicts have specific meanings. The Phase 12 operator manual carries the explanation.
- The `unknown` state is a real operator-facing surface. Today, "unknown" is implicit (gates not consulted reactively); explicitly surfacing it may feel like *new* uncertainty when in fact it has always been present. The trade-off is intentional — an explicit `unknown` is honest, an implicit `paper-only` is not.

**Non-consequences:**

- The four-layer `place_paper_order` enforcement is unchanged. The verdict gate is a higher-altitude refusal that fires before sizing resolution; the lower-altitude enforcement remains as defense in depth.
- The connection-time multi-account refusal (`managedAccounts() > 1`) is unchanged.
- Phase 3's strict `ledger.account_id == broker.connected_account` match is a sibling gate, not absorbed by this ADR — it checks identity match; this ADR checks prefix shape.
- ADR 0009's audit-copy allow-list is unchanged. The QC provenance card consumes its output; this ADR does not redefine the proof.
- The readiness gate's overall shape (`{verdict, summary, gates: […]}`) is unchanged — this ADR adds one hard input.
