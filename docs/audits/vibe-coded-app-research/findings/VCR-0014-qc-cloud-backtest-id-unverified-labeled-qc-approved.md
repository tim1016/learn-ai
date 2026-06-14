---
id: VCR-0014
severity: P2
status: open
area: ui-runtime-claims
canonical_file: Frontend/src/app/components/broker/broker-provenance-card/broker-provenance-card.component.ts:48
reference: docs/architecture/adrs/0006-deploy-control-plane-host-daemon-init-ledger.md
first_seen: 2026-06-14
last_seen: 2026-06-14
lens: run-ledger-identity-provenance
dedupe_with_F: none
confidence: high
---

## What

The run ledger pins three things to give the operator confidence the live algorithm matches a QC Cloud backtest: `code_sha` (clean-tree-gated), `qc_audit_copy_sha256` (computed from the on-disk file), and `qc_cloud_backtest_id` (operator-supplied free-form string). The first two have engine-derived receipts; `qc_cloud_backtest_id` has none — ADR 0006 § "QC anchor" explicitly acknowledges there is no QC-cloud API in the repo and that the operator supplies the id as a string.

That is the documented design. The gap is in the Provenance card frontend (`broker-provenance-card.component.ts:48-54`), which renders this string as a **positive proof** row labelled "QC-approved" with the statement *"Byte-identical to backtest ${p.qc_cloud_backtest_id} — audit copy ${filename(p.qc_audit_copy_path)}"*. The wording implies the backtest id has been verified to correspond to the audit copy. The system has never checked that link.

Three concrete failure modes: (a) operator typo — wrong id displayed forever as the QC-approved anchor; (b) operator pastes an unrelated old backtest id from a different algorithm — the system silently writes it into the audit trail; (c) the QC-side backtest id can be deleted, renamed, or re-run by anyone with access to QC Cloud, and the ledger still claims it as the proof.

## Where

- `Frontend/src/app/components/broker/broker-provenance-card/broker-provenance-card.component.ts:48-54` — "QC-approved" label + "Byte-identical to backtest" statement.
- `PythonDataService/app/schemas/live_runs.py:318` — `HostRunnerDeployRequest.qc_cloud_backtest_id: str = Field(min_length=1)` — only length check.
- `PythonDataService/app/engine/live/run_ledger.py:16-18` — module docstring is honest that this is operator-supplied.
- `docs/architecture/adrs/0006-deploy-control-plane-host-daemon-init-ledger.md:62-66` — ADR explicitly defers the QC-Cloud API integration; the operator-supplied caveat is in the spec.

## Why this severity

PRD §7 P2: moderate auditability. Not P1 because the audit_copy SHA *is* a real receipt — the algorithm-vs-backtest mapping is fully testable from that fingerprint. But the Provenance card phrasing implies more verification than exists, which is the operator-decision-quality concern P1 covers; the actual mis-trade risk is low because reconciliation downstream would detect a mismatched backtest id.

## Trading impact

An operator could deploy a `SetHoldings(1.0)` Reference parity run anchored to the wrong backtest id and trust the Provenance card's "QC-approved" badge. When reconciliation later fails, the operator's first reaction is "my engine has a bug", not "the ledger names the wrong backtest". In a multi-strategy / multi-operator environment this becomes a real source of misattributed P&L and burned reconciliation effort.

## Reproduction

```bash
sed -n '48,54p' Frontend/src/app/components/broker/broker-provenance-card/broker-provenance-card.component.ts
grep -n 'qc_cloud_backtest_id' PythonDataService/app/schemas/live_runs.py
```

## Suggested resolution (NOT auto-applied)

Reword the proof row to be honest. Either:

1. Split the row into two: *"Audit copy: byte-identical to ${filename} (sha verified)"* + *"Operator-recorded QC backtest: ${id} (not auto-verified)"*. The first is true; the second discloses the operator-supplied status.
2. Replace the "QC-approved" label with "QC reference" and the statement with *"Tagged to QC backtest X — link not verified by the engine"*.

A future enhancement could implement the deferred QC-Cloud API integration per ADR 0006 § "Alternatives, recorded".

## Provenance of the finding

Lens: `run-ledger-identity-provenance` (workflow `wf_def78013-ce4`, structured-finding `qc-cloud-backtest-id-unverified-but-labeled-qc-approved`, verified 2/2 by adversarial pass).
