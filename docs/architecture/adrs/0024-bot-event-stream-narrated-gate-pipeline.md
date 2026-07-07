# ADR 0024 — Bot event stream: narrated gate pipeline over a generalized broker-activity stream

**Status:** Accepted 2026-07-06. Drafted during a `grill-with-docs` design pass on order-path error surfacing.
**Extends:** ADR-0014 (broker-authored operator view — backend-rendered narratives), ADR-0015 (operator notice contract).
**Related:** ADR-0008 (durable submit protocol — `order_ref` ownership, run-scoped WAL), ADR-0011 (broker safety verdict — reactive, fail-closed), ADR-0013 (operator-surface boundary: judgment vs evidence).
**Decision driver:** Operator instruction — *"most of the places are gates and channels through which an order propagates, and errors emanate in these channels but are lost in the chain. At each gate, find the last threaded error, lift it, and propagate it back to the UI in a user-friendly way — all authored by the backend."*

## Context

The order-submission path predates the ADR-0015 notice contract and was never retrofitted to it. Three structural failures follow:

1. **The most-granular error is thrown away at the seam.** `LivePortfolio` catches the broker submit exception generically and flattens it to a string — `ack_reason = f"broker.place_order raised: {type(exc).__name__}: {exc}"`. A structured `OrderRefusedError` (readonly lockdown, non-DU account, missing `order_ref`, contract-qualify timeout) becomes an opaque string in a WAL field; the specific "what actually failed" is lost.

2. **IBKR's own error is captured, then dies unjoined.** When IBKR rejects an order it fires `errorEvent(reqId, errorCode, errorString)`. We capture code + string into `connection_events.jsonl` and already own a 20+ entry `IBKR_CODE_MEANINGS` table (`event_codes.py`) mapping e.g. `201 → ("order rejected", critical)`. But there is **no `reqId → order_ref` join**, the code never reaches the WAL/ack, and the broker-activity row classifies the rejection `verdict=expected, reason=rejection` with the IBKR code and string *stripped*. The single most useful fact — IBKR's own *"order rejected: insufficient buying power"* — sits in a log the operator never opens.

3. **Five inconsistent terminal surfaces; only one does it right.** An order/bot error terminates on `run_status.json`/`InstanceLastExit`, `broker_activity.jsonl` rows, `operator_surface` notices, `submit_readiness.reason_code` (free-text), or just `connection_events.jsonl`/logs. Only the **IBKR client-id collision on launch** captures max granularity *and* surfaces it well (`run_status.json` `exit_error_code`/`exit_error_message`/`exit_error_detail` → `InstanceLastExit`). That is the reference pattern; the rest lose their error or defer it to the next cold-start reconciliation.

Compounding this: pre-order blocks (stale market data, session closed, safety verdict, no signal) have **no home** because no `intent_id` exists yet — so the operator's most common question, *"why isn't my bot trading right now?"*, has no answer surface at all.

## Decision

**Introduce a per-bot _Bot event stream_ that narrates the strategy instance's live pipeline — bar evaluation → gates → order → broker outcome — as a sparse, backend-authored, drill-in row feed. It generalizes the ADR-0014 broker-activity stream upstream: broker executions become its terminal event-type, not a separate channel. At each gate the enforcement point captures its outcome once; a terminal failure carries the most-granular error captured at that exact gate, authored into operator-facing copy with the exact error preserved as forensic evidence.**

### 1. Terminal error = most granular at the failing gate

The surfaced error is the **most-granular error captured at the exact gate where the evaluation or order failed**, preferring the external system's native error (IBKR `errorCode`/`errorString`, subprocess exit + stderr, OS errno) over any wrapper the engine puts around it. The `__cause__` chain is walked to its innermost link. The operator sees a backend-authored *useful derivation* (title/message); the exact error is kept as expandable forensic evidence. (Codified as **Terminal error** in `CONTEXT.md`; supersedes the fuzzy "last threaded error" — it means *most granular at the failing gate*, not outermost or most-recent.)

### 2. One narrated stream, not five surfaces

A single per-bot **Bot event stream** is the unifying surface. Errors are no longer lost in the *inconsistency between chains* because there is one chain.

### 3. Sparse authored spine + drill-in

The stream is a **sparse spine** of authored, visible rows; quiet bars fold to a single `evaluation_idle` heartbeat and never scroll. **Every row is expandable** to its full gate-walk (`sizing ✓ → broker-safety ✓ → daily-cap ✗`) plus the raw external error. Progress and error share one folded-row-with-drill-in shape (reuses ADR-0014's "authored row + expandable forensic facts"). The spine includes `order_cancelled` as a non-escalating broker-tail outcome so ADR-0014 cancellation rows have a lossless replacement-map target.

### 4. Evaluation-spine with order-cluster promotion (amends ADR-0014 §1)

A row **starts** keyed to its **evaluation** (so pre-order blocks have a home) and is **promoted** to the order's `order_ref` identity the moment an intent is minted — one unbroken row from *bar evaluated → signal → gates → submitted → filled/rejected*. This **amends ADR-0014's "row = one IBKR execution" verbatim rule**: the row is now the evaluation→order→execution cluster; a broker execution is the last event-type on it.

This is a **new versioned contract — `BotEventRow` (authored projection) over `BotEventRaw` (raw capture), with `GateStep` and `TerminalError` as child shapes — not an in-place mutation of `BrokerActivityRow`**, whose "one IBKR execution (or one engine-only-pending intent)" identity is a load-bearing docstring contract for ADR-0014 consumers. Broker executions enter the new contract as terminal child event-types; the old row model maps into the stream tail via an explicit replacement map (PRD #928 Slice -1). ADR-0014's backend-rendered-narrative rule, closed verdict enum, and template-truthfulness contract are unchanged and carry forward to the broker event-type.

### 5. Two altitudes, terminal-outcomes escalate

Most events wait to be found in the stream. **Terminal outcomes** (`halted`, `order_rejected`, `launch_failed`, submit-uncertain) *also* mint an `OperatorIncident` (ADR-0015) so the cockpit `incident_headline` + page-wide auto-expand surface them even when the operator is not watching the stream. **Self-protective, expected blocks** (market closed, no signal, session halted) stay in-stream at `info` — escalating those trains alarm fatigue. Escalated notices honor the error-authoring rule: `action.kind` only when a real fix exists; `external_manual_check`/`none` for the genuinely-uncertain (never a fabricated "try again").

Two consequences are explicit so implementers do not soften them:

- **The rejection break.** A broker rejection stays *expected as a broker-callback shape* (the callback plumbing behaved normally) but becomes *terminal and attention-worthy as an operator outcome*. The existing `verdict=expected, reason=rejection` activity row is **replaced** by the `order_rejected` terminal event — do not leave the old expected row in place and add a notice beside it.
- **One visible terminal story.** `OperatorIncident.category` — closed today at `watchdog | activity | reconciliation` — gains an order/submit member, and every terminal outcome declares a **dedupe key** (strategy instance + `order_ref`/`evaluation_id` + terminal code) so one failure yields exactly one incident and one stream row, never duplicates across stream and incident headline.

### 6. Classifier-only emission seam — no submit control-flow rewrite

The order path already has exactly two gate shapes, both already closed sets. The seam matches them and rewrites **no** ADR-0008 control flow:

- **Structured-verdict classifier.** Gate-steps are **raw-captured at enforcement time** — one event per gate traversed, carrying `evaluation_id`, `gate_id`, `gate_result`, and `source_authority` — and the classifier authors from those captured events. The historical walk is never reconstructed from the readiness sidecar: `readiness_gates` is a *"can it act on the next bar?"* current-state vector, not a history log. The vector the engine enforces and the events it emits are outcomes of the same evaluation, so the walk cannot drift from enforcement; the readiness verdict becomes the *now*-projection of the same emissions.
- **Exception/IBKR classifier.** The closed `ControlledLiveHaltError` hierarchy (`BrokerSafetyVerdict` / `AccountFreeze` / `AccountRegistry` / `AccountTruth` / `SubmitUncertain`) + `BrokerError`/`OrderRefusedError` + `IBKR_CODE_MEANINGS` → a terminal notice, `__cause__`-innermost, external-native fields extracted.

Anti-drift is enforced by an **exhaustiveness snapshot test** over both closed sets (extending ADR-0015's exhaustiveness gate). An unmapped exception or IBKR code renders a **visible "unmapped diagnostic"** notice — never guessed, never swallowed.

### 7. One source, two projections

Gate outcomes are **authored once at the enforcement point** per evaluation. The `operator_surface` readiness verdict renders the **current** "can it trade now" summary — contract unchanged. The Bot event stream renders the **historical walk** over time. Neither re-derives the other's verdict (honors ADR-0013 and CLAUDE.md single-source-of-truth #5). Everything else — activity table, working orders, incident headline — is a projection that drills into one of these two surfaces; no third surface independently explains truth (see §9).

### 8. Persistence — raw capture first, authored projection second

Following the ADR-0014 two-stage model: the **enforcement point** — the runtime that *enforces or observes* the gate — appends **raw** gate/terminal events to a run-scoped WAL (`bot_events.jsonl`, fsync-before-return, peer to `intent_events.jsonl` and `broker_callbacks.jsonl`). For evaluation and submit gates that runtime is the engine loop; for spawn failures and subprocess stderr it is the daemon/launcher; for broker session collisions it is the broker session layer. "Engine-authored" is the common case, not the invariant — the invariant is *enforcement-point-owned capture, publisher-authored projection*. The data-plane publisher **projects/authors** spine + gate-step rows into an authored WAL, fans them out over SSE, and serves REST backfill. The publisher is a projector, never the authority for whether a gate fired; the authored projection is deterministically rebuildable from the raw WAL. This makes "broadcast if needed" honest both ways: live for when the operator is watching, durable + backfill for when they were not.

### 9. Surface disposal — replace, don't add

The stream refuses to become a sixth surface beside the five it indicts. Exactly **one current-verdict surface** (the `operator_surface` readiness verdict) and **one historical stream** (the Bot event stream) exist; every other operator surface is a projection over one of these two, or is deleted:

| Surface today | Disposition |
|---|---|
| Broker Activity table | **Retained as projection** — the stream's broker-tail *filtered view*. The name may survive; the standalone product concept does not. |
| Working / pending orders | **Retained as projection** of the stream's open order-clusters. |
| `verdict=expected` rejection row | **Deleted** — replaced by the `order_rejected` terminal event (§5). |
| Cockpit readiness gate display | **Retained** — the one verdict-now surface; contract unchanged, now the *now*-projection of the same enforcement-time emissions (§6). |
| Cockpit `incident_headline` | **Retained as projection** — the escalation view of stream terminal outcomes, deduped by incident key (§5). |
| Any UI-only gate checklist not backed by the exact enforcement predicate result | **Demoted or deleted** — reaffirms the gate-map review rule that parallel gate-board projections are out of scope. |

The gates themselves are untouchable — they are the safety model. What is consolidated is their *visualization*, never their enforcement. Removal is deliberate: destructive consolidation in service of truth beats preserving surfaces that can disagree. A surface that cannot be re-founded as a projection of the stream or the verdict is deleted, not maintained beside its replacement.

## Consequences

**Positive:**
- The operator's two core questions — *"why isn't my bot trading?"* and *"where exactly did that order die, and what was the most granular error we had?"* — are answerable from one surface, live, with the external system's own error authored into plain language.
- Errors can no longer be lost in the inconsistency between five surfaces; there is one chain, and the anti-drift guarantee rides on exhaustiveness tests over two already-closed sets.
- No rewrite of the correctness-critical ADR-0008 submit machine — the seam is classification, not control flow.
- Reuses shipped infrastructure (ADR-0014 per-instance SSE publisher, ADR-0015 notice contract, the `IBKR_CODE_MEANINGS` table, the `run_status.json` structured-exit pattern) rather than inventing a sixth surface.
- Surfaces that could disagree are deleted or demoted to projections (§9); disagreement between operator surfaces becomes structurally impossible rather than operationally policed.

**Negative:**
- Adds an `order.*`/`submit.*` namespace to the `OperatorNoticeCode` union and a new run-scoped WAL — more schema surface and a snapshot test to maintain.
- The `reqId → order_ref` join is net-new correctness work (it does not exist today) and is a prerequisite for attributing IBKR errors to the order.
- The Activity-tab row model is re-founded on the versioned `BotEventRow` contract (amends ADR-0014 §1) — a breaking migration with an explicit replacement map, not an informal extension; the render component and its truthfulness property test must extend to the new event-types.
- The versioned `BotEventRaw`/`BotEventRow` contract, the identity ladder, the replacement map, and the incident dedupe keys are net-new contract work that must land before any behavior slice (PRD #928 Slice -1).

**Non-consequences:**
- ADR-0008 `LiveStateEnvelope` semantics and the durable submit protocol are unchanged; the engine gains event-emission calls, not new submit control flow.
- ADR-0013's operator-surface boundary is reinforced: the stream is evidence on its own channel; the cockpit readiness verdict remains the single authority for "can it trade now."
- Out-of-band push (browser notification / email) for critical halts is explicitly **not** decided here — deferred pending a delivery-channel decision.

## References

- `CONTEXT.md` § "Bot event stream — narrated gate pipeline (resolved 2026-07-06)" — canonical vocabulary.
- ADR-0014 §1 (verbatim rule, amended by §4 above), §4–5 (raw-capture-then-authored-projection, reused).
- ADR-0015 (operator notice contract; `order.*`/`submit.*` namespace extends its `OperatorNoticeCode` union and exhaustiveness gate).
- `IBKR_CODE_MEANINGS` (`app/broker/ibkr/event_codes.py`) — the code→meaning table reused for authoring, never forked.
- The `run_status.json` `exit_error_{code,message,detail}` → `InstanceLastExit` path — the reference pattern generalized to every terminal outcome.
- `docs/architecture/bot-lifecycle-gate-map.md` § Review Update ("gate board drift risk") — prior art for the no-parallel-gate-projection rule reaffirmed in §9.
