import { describe, expect, it } from 'vitest';

import { formatReceiptLabel, formatReceiptValue, ReceiptLabelPipe } from './receipt-label.pipe';

describe('formatReceiptLabel', () => {
  it('formats underscore, dot, dash, and uppercase receipt identifiers as title case', () => {
    expect(formatReceiptLabel('NO_LIVE_BINDING')).toBe('No Live Binding');
    expect(formatReceiptLabel('readiness')).toBe('Readiness');
    expect(formatReceiptLabel('broker.connection')).toBe('Broker Connection');
    expect(formatReceiptLabel('host-process.disabled_reason_code')).toBe(
      'Host Process Disabled Reason Code',
    );
    expect(formatReceiptLabel('FAILED')).toBe('Failed');
  });

  it('preserves known acronyms', () => {
    expect(formatReceiptLabel('ibkr_api_evidence')).toBe('IBKR API Evidence');
    expect(formatReceiptLabel('intent_wal_pnl')).toBe('Intent WAL P&L');
    expect(formatReceiptLabel('RTH')).toBe('RTH');
  });

  it('renders the internal clerk event kind as Account service language', () => {
    expect(formatReceiptLabel('clerk')).toBe('Account service');
    expect(formatReceiptLabel('account_clerk')).toBe('Account service');
  });

  it('formats comma-separated code lists', () => {
    expect(formatReceiptLabel('COMMAND_LOOP_STALE, CONTROL_PLANE_LEASE_STALE')).toBe(
      'Command Loop Stale, Control Plane Lease Stale',
    );
  });

  it('renders the governed Signal Program canary vocabulary as operator language', () => {
    expect(formatReceiptLabel('CANARY_PAIRING_NOT_ALLOWLISTED')).toBe(
      'Canary Pairing Not Allowlisted',
    );
    expect(formatReceiptLabel('CANARY_ROLLBACK_REQUIRES_FLATTEN')).toBe(
      'Canary Rollback Requires Flatten',
    );
    expect(formatReceiptLabel('CANARY_ROLLBACK_BOUNDARY_UNPROVABLE')).toBe(
      'Canary Rollback Boundary Unprovable',
    );
    expect(formatReceiptLabel('PROGRAM_BUILD_UNPROVEN')).toBe('Program Build Unproven');
    expect(formatReceiptLabel('CANDIDATE_UNCAPTURED_AT_CRASH')).toBe(
      'Candidate Uncaptured At Crash',
    );
  });

  it('renders authority, provenance, and verification vocabulary', () => {
    expect(formatReceiptLabel('real_paper')).toBe('Real Paper');
    expect(formatReceiptLabel('synthetic')).toBe('Synthetic');
    expect(formatReceiptLabel('clerk_decision_receipt')).toBe('Clerk Decision Receipt');
    expect(formatReceiptLabel('legacy_simulated_wal')).toBe('Legacy Simulated WAL');
    expect(formatReceiptLabel('live_reproof')).toBe('Live Reproof');
    expect(formatReceiptLabel('frozen_run_evidence')).toBe('Frozen Run Evidence');
  });

  it('leaves backend prose untouched', () => {
    expect(formatReceiptLabel('Broker snapshot disagrees with the intent WAL.')).toBe(
      'Broker snapshot disagrees with the intent WAL.',
    );
  });

  it('preserves opaque audit token values based on the receipt label', () => {
    expect(formatReceiptValue('intent_id', 'intent-7')).toBe('intent-7');
    expect(formatReceiptValue('intent-id', 'intent-7')).toBe('intent-7');
    expect(formatReceiptValue('runbook.url', 'https://example.test/runbook?id=7')).toBe(
      'https://example.test/runbook?id=7',
    );
  });

  it('formats code-like receipt values when the label is not opaque', () => {
    expect(formatReceiptValue('state', 'NO_LIVE_BINDING')).toBe('No Live Binding');
    expect(formatReceiptValue('source', 'readiness')).toBe('Readiness');
    expect(formatReceiptValue('attempt_count', 3)).toBe('3');
    expect(formatReceiptValue('available', true)).toBe('True');
  });

  it('supports the Angular pipe wrapper', () => {
    expect(new ReceiptLabelPipe().transform('already_running')).toBe('Already Running');
  });
});
