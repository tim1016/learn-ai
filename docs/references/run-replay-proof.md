# Run-scoped replay proof (Direction 2)

**Spec:** `docs/audits/strategy-execution-research-directions-2026-08-24.md` (Direction 2)
**Shipped:** PRD #1753, slices #1758–#1762 · **Status:** active for every Paper (`trade`) and Dry Run stop

## What it is

Every paper/dry run retains its exact source observations in an
instance-scoped `SourceBarLedger` (`paper:<sid>` / `sim:<sid>` under
`accounts/alpaca/`) and produces a durable `RunReplayReceipt` at
`live_state/<sid>/run_replay_receipts/<run_id>.json` with two proofs:

1. **Engine parity** — `run_shadow_trace_evaluation`
   (`app/broker/alpaca/clerk/sqlite/qualification_shadow_trace.py`) over the
   run's retained stream, **bounded at the run's durable end** (the
   `ledger_end_seq` snapshot taken at Stop, or the terminal outcome's
   `recorded_at_ms` for crashed/legacy runs): BacktestEngine vs the shared
   runner seam, all-COMMIT, fails on the first divergent trace field.
2. **Run fidelity** — a disposition-faithful replay through the production
   `strategy_evaluations` generator, aligned to the run's decision receipts
   by deterministic `evaluation_id` and verified **content-by-content**
   against each receipt's live-captured `trace_digest`
   (`trace_root([trace])`), settling each stage with the live-recorded
   disposition so a legitimately-refused intent never cascades into false
   drift. `digest_verified_count` discloses coverage; digest-less rows
   (recorded before capture existed) fall back to intent-kind comparison —
   the receipt's stated residual blind spot.

## When a receipt is generated (coverage contract)

- **Operator Stop** — scheduled at Stop (`pending` written synchronously,
  compute in the background).
- **Stream end / feed death / in-process crash** — scheduled from the
  supervisor's three non-cancel terminal branches.
- **Process death** — the boot-recovery sweep re-schedules orphaned
  `pending` receipts and terminal current runs that never got one.
- **Older historical runs** — on demand only, via
  `POST /api/brokers/{broker}/bots/{sid}/runs/{run_id}/replay-receipt`.

## Divergence classification (deliverable 3)

Alignment is keyed on the deterministic `evaluation_id` (a dict lookup, not
receipt order), so one missing mid-journal receipt is a single localized
divergence, not a cascade.

| classification | reason codes | meaning |
|---|---|---|
| `expected_live_effect` | a `blocked` row whose reason is in the CLOSED set `EXPECTED_LIVE_GATE_REASON_CODES` (liveness gate, `PAUSED_OBSERVE_ONLY`, stream-health hold, Clerk pre-custody refusal, and the transient admission refusals `TRANSIENT_ADMISSION_REASON_CODES`) AND whose trace digest matched the replay; a digest-verified `CANDIDATE_UNCAPTURED_AT_CRASH` crash-window row | live-only gates the mode-parity seam documents (`tests/engine/strategy/test_signal_program_mode_parity.py`); math agreed, custody legitimately refused — a `blocked` row is cross-checked, never trusted on presence, and a crash row is digest-verified against its reconstructed candidate |
| `drift` | `TRACE_DIGEST_MISMATCH`, `DECISION_MISMATCH`, `UNRECOGNIZED_BLOCK_REASON`, `MISSING_LIVE_RECORD`, `UNMATCHED_LIVE_RECORD` | replayed math and durable live record disagree with no enumerating live effect — a real bug, treat like a failed reconciliation |

Content drift (a mismatch on a retained aligned row) is separated from
absence drift (`MISSING`/`UNMATCHED` alignment gaps): content drift is the
loudest verdict and dominates even a truncated window, while absence drift is
a real-drift verdict only on a **complete** journal — a truncated window
degrades absence drift to `indeterminate` (retention loss is never promoted
to drift).

Statuses: `pending` → `parity` | `parity_with_expected_live_effects` |
`indeterminate` | `drift` | `replay_failed`. Verdict ordering: proven content
drift is the loudest; known-incomplete evidence (`records_truncated`) or an
unprovable engine leg yields `indeterminate` — partial evidence never earns
a proof verdict.

## Known bounds (documented, not hidden)

- **Receipt-retention truncation:** a run longer than
  `MAX_DECISION_RECEIPTS_PER_STRATEGY` decisions has its earliest rows pruned,
  sets `records_truncated`, and forces the verdict to `indeterminate` — never
  `parity`. The retention floor test pins that a normal daily run never hits
  this.
- **Digest coverage:** rows recorded before live-time trace-digest capture
  existed verify at intent level only; `digest_verified_count` vs
  `live_compared_count` discloses exactly how much of the run was
  content-verified.
- **Ledger capacity:** `SOURCE_BAR_STREAM_CAPACITY` (200k bars/stream) fails
  closed per #1740; a months-old instance needs a reviewed rollover before
  its next run can retain.
- **Disaster-recovery backup:** the instance-scoped evidence ledger
  (`paper:<sid>` / `sim:<sid>`) is not part of the Clerk verified-backup
  workflow (that workflow covers established custody accounts only, and these
  evidence namespaces are deliberately not Clerk accounts — the same stance
  Dry Run has always had). A restore into a fresh artifact root recovers
  custody but not the raw bars needed to *regenerate* a paper-run replay; the
  per-run receipt itself is durable under `live_state/`. Backing up evidence
  ledgers is a separate concern (it would apply equally to Dry Run) and is out
  of scope for this PRD.
- **No admission coupling:** receipts are evidence out, never a gate in — the
  Paper evidence-only override is permanent by operator decision (spec §
  "Standing constraint").
