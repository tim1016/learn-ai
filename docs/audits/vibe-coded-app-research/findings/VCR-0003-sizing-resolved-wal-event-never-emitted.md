---
id: VCR-0003
severity: P1
status: partially_remediated
area: live-sizing
canonical_file: PythonDataService/app/engine/live/intent_events.py:46
reference: docs/architecture/adrs/0009-live-sizing-authority-and-provenance.md (§ 11)
first_seen: 2026-06-14
last_seen: 2026-06-14
remediation_progress:
  - "#530 — Phase 8 — SIZING_RESOLVED emitted right after intent_id mint in LivePortfolio.set_holdings (before PENDING_INTENT)"
  - "PR feat/phase-8-sizing-skip — Phase 8 SIZING_SKIP durable audit log: separate sizing_skip.jsonl file alongside intent_events.jsonl. PRD §8 schema (no intent_id; carries target/current qty + reason). The IntentEvent invariant (order_ref==namespace:intent_id) is preserved — skips truly are not intents and live in their own log per the autonomous architectural decision; the in-memory sizing_resolutions list still annotates the skip row with a 'skipped' marker so the Sizing card stays consistent."
follow_up_required:
  - "Sizing card data-source cutover from in-memory sizing_resolutions list to a fold of SIZING_RESOLVED (intent_wal) + SIZING_SKIP (sizing_skip.jsonl) (PRD §8 step 6); deferred"
  - "ADR 0009 § 6 reverse order-surface validation: policy-registered strategy invoking market_order should fail fast (VCR-P3-F)"
lens: live-sizing-adr-0009
dedupe_with_F: none
confidence: high
---

## What

ADR 0009 Decision 11 (*Per-trade audit — a new `SIZING_RESOLVED` WAL event*) specifies a new durable event:

> A new `SIZING_RESOLVED` event in `intent_events.jsonl`, appended **before** `SUBMITTED` / `ACK_FAILED_UNCERTAIN`, carrying `{ts_ms_utc, intent_id, policy_kind, policy_value, intended_qty, reference_price (str/decimal), sizing_provenance_at_resolve_time, sized_via}`. The event captures the sizing decision at the moment the order was constructed — not at fill time, not at session boundary.

The event is **declared and fold-supported**, but **never written** by production code:

- `intent_events.py:46` defines `IntentEventType.SIZING_RESOLVED`.
- `intent_wal.py:72` includes a comment carving it out from the SUBMITTED/PENDING fold contract.
- `intent_ledger.py:108-141` folds the event when it arrives — but the fold is hypothetical because no producer exists.
- A repo-wide grep for `IntentEventType.SIZING_RESOLVED` returns only those two source-of-truth declarations. There is **no** `wal.append(event_type=IntentEventType.SIZING_RESOLVED, ...)` call anywhere.

What does exist in its place: `live_state_sidecar.py:81` declares `sizing_resolutions: list[dict[str, Any]]` and `run.py:687-721` writes a periodic snapshot of the sidecar with the accumulated resolutions. That is a best-effort projection of the per-trade audit, not the durable, WAL-ordered, intent_id-keyed event the ADR specifies. Two material gaps from the ADR contract:

1. **No `intent_id` linkage**: the sidecar `sizing_resolutions` rows do not carry `intent_id`, so they cannot be joined to broker fills the way the ADR's "per-trade audit list" requires.
2. **No `sizing_provenance_at_resolve_time`**: the ADR explicitly names this field on the WAL event (so a later policy mutation is forensically attributable). The sidecar dict does not currently capture it.

The Sizing card's per-trade audit table on the cockpit therefore renders sidecar best-effort snapshots, not durable WAL evidence. Survives crash because the sidecar is periodic, but cannot reconstruct an arbitrary `(fill → policy at the moment of order construction)` join.

## Where

- `PythonDataService/app/engine/live/intent_events.py:46` — `IntentEventType.SIZING_RESOLVED = "SIZING_RESOLVED"` declaration.
- `PythonDataService/app/engine/live/intent_events.py:28,88` — docstrings describing the event.
- `PythonDataService/app/engine/live/intent_wal.py:72` — fold contract carve-out for SIZING_RESOLVED.
- `PythonDataService/app/engine/live/intent_ledger.py:108-141` — fold consumer (hypothetical: no upstream producer exists).
- `PythonDataService/app/engine/live/live_state_sidecar.py:81` — `sizing_resolutions: list[dict]` (sidecar projection).
- `PythonDataService/app/engine/live/run.py:687-721` — periodic snapshot write; commit at `:716-721` clears `portfolio.sizing_resolutions` only after `repo.write` succeeds (the reviewer fix referenced in lens summary).
- **Negative grep**: no call site for `wal.append(event_type=IntentEventType.SIZING_RESOLVED, ...)` anywhere under `PythonDataService/app/`.

## Why this severity

PRD §7 P1: "critical behavior exists without tests/provenance" and "UI implies guarantees the backend/runtime does not enforce".

The Sizing card's per-trade audit promises a verifiable join from fills back to the sizing policy that produced them. The current implementation provides a periodic sidecar snapshot — adequate as a UI table but **not** the durable, ordered, intent-keyed evidence the ADR commits to. A future operator reviewing "why did this fill carry sizing FixedShares(3) when the active policy is FixedShares(1)?" cannot answer the question from the current artifact set.

Not P0 because (a) the audit is not silently corrupted, just structurally insufficient for the ADR contract, and (b) the headline sizing safety (Safe canary default, all-in coexistence guard, fail-closed Reference parity) is independent of the per-trade audit's durability shape. But the ADR's framing of the per-trade audit as a load-bearing operator surface elevates this above P2.

## Trading impact

No direct order or sizing impact today — the policy still resolves correctly; only the durable trail is incomplete. Indirect impact:

- **Post-restart forensics weak**: a sidecar snapshot may be up to N seconds stale at crash time, so the fills-since-last-snapshot have no recoverable policy linkage.
- **Order-surface fail-fast (ADR 0009 § 6) is harder to attribute** after the fact: when the fail-fast fires, the operator gets the error, but reconstructing whether earlier orders in the same session were `policy_set_holdings` or `strategy_explicit_market_order` (the `sized_via` taxonomy) requires the WAL event, not the snapshot.
- **Cross-policy redeploys lose history**: when the operator redeploys with a fresh `run_id`, the sidecar rotation at `run.py:687-689` discards prior policy resolutions. The WAL event would have been read-only history.

## Reproduction

```bash
# Show the event is declared:
grep -n "SIZING_RESOLVED" PythonDataService/app/engine/live/intent_events.py

# Show the fold consumer exists:
sed -n '108,141p' PythonDataService/app/engine/live/intent_ledger.py

# Show NO production code writes it (only the two source-of-truth declarations match):
grep -rn "IntentEventType.SIZING_RESOLVED" PythonDataService/app/

# Show the sidecar is the only actual per-trade audit storage:
grep -n "sizing_resolutions" PythonDataService/app/engine/live/live_state_sidecar.py
sed -n '687,721p' PythonDataService/app/engine/live/run.py
```

## Suggested resolution (NOT auto-applied)

1. In `OrderSizer.resolve_set_holdings_quantity` (and the FixedShares/FixedNotional paths), emit `wal.append(event_type=IntentEventType.SIZING_RESOLVED, payload={...})` carrying `{ts_ms_utc, intent_id, policy_kind, policy_value, intended_qty, reference_price (Decimal as str), sizing_provenance_at_resolve_time, sized_via}` per the ADR.
2. Sequence the WAL write **before** the `PENDING_INTENT` / `SUBMITTED` event (ADR § 11 ordering).
3. Migrate the Sizing card's per-trade audit query from `live_state.json::sizing_resolutions` to a fold over `intent_events.jsonl` filtered by `event_type == SIZING_RESOLVED`. The sidecar field can remain as a fast-path cache, but the WAL is the source of truth.
4. Add a parity test that asserts every fill recorded in `executions.parquet` joins to exactly one `SIZING_RESOLVED` event via `intent_id`, and that `sized_via` matches the recorded `governed_by`.

## Provenance of the finding

Lens: `live-sizing-adr-0009`. Verified by direct grep of all production code for `IntentEventType.SIZING_RESOLVED`: only the declaration in `intent_events.py:46`, the docstring at `:28`, the comment at `intent_wal.py:72`, and the fold consumer at `intent_ledger.py:108-132` match. No `wal.append(event_type=…SIZING_RESOLVED…)` exists.
