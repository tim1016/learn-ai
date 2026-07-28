import type { AccountSafetySnapshot } from '../app/api/broker-models';

export function makePresentedReconcileAction(): NonNullable<AccountSafetySnapshot['actions']>[number] {
  return {
    action_id: 'reconcile_now',
    target: { account_id: 'DU1234567' },
    snapshot_id: 'b'.repeat(64),
    snapshot_version: 'a'.repeat(64),
    evidence_refs: [],
    effect_class: 'EVIDENCE_REFRESH',
    idempotency_key: 'c'.repeat(64),
    issued_at_ms: 1_780_000_000_000,
    expires_at_ms: 1_780_000_060_000,
    presentation_token: 'd'.repeat(64),
    preconditions: [
      { code: 'account_safety.verdict', expected_value: 'RECONCILING' },
    ],
    confirmation: {
      title: 'Run account reconciliation',
      body: 'Request a fresh account reconciliation for this account.',
      consequence: 'The server will revalidate this snapshot before it refreshes account evidence.',
      confirm_label: 'Run account reconcile',
      required_token: '',
    },
    availability: 'AVAILABLE',
    disposition: 'fix_here',
    finished_copy: 'Reconciliation request accepted. Refresh account safety evidence for its observed result.',
  };
}

export function makeAccountSafetySnapshot(
  overrides: Partial<AccountSafetySnapshot> = {},
): AccountSafetySnapshot {
  return {
    schema_version: 1,
    snapshot_version: 'a'.repeat(64),
    snapshot_id: 'b'.repeat(64),
    generated_at_ms: 1_780_000_000_000,
    account_id: 'DU1234567',
    posture: 'PAPER_EXECUTION',
    verdict: 'RECONCILING',
    reason_code: 'ACCOUNT_RECONCILIATION_REQUIRED',
    verdict_reason: 'Fresh reconciliation is required before new entry risk can proceed.',
    account_epoch: { clerk_boot_id: 'boot-1', epoch_seq: 4 },
    reconciliation_id: 'reconcile-1',
    reconciliation_state: 'NOT_PROVEN',
    sources: [
      {
        source: 'account_truth',
        state: 'STALE',
        critical: true,
        epoch_relation: 'CURRENT',
        as_of_ms: 1_779_999_990_000,
        age_ms: 10_000,
        hard_ttl_ms: 5_000,
        reason_code: 'ACCOUNT_TRUTH_STALE',
        evidence_refs: [],
      },
    ],
    custody: {
      retired_owner_custody_count: 0,
      clerk_journal_last_seq: 42,
      a0_custody_accepted_count: 1,
      a1_broker_write_started_count: 1,
      a2_broker_known_count: 0,
      a3_economic_terminal_count: 4,
    },
    exposure: {
      open_order_count: 1,
      execution_count: 4,
      position_count: 1,
      unmanaged_or_unknown_count: 1,
    },
    blockers: [],
    outage_diff: null,
    evidence_refs: [],
    actions: [],
    ...overrides,
  };
}
