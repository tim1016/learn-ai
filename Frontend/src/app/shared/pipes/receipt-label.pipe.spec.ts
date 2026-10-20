import { describe, expect, it } from 'vitest';

import { formatReceiptLabel, formatReceiptValue } from './receipt-label.pipe';

describe('formatReceiptLabel', () => {
  it('turns receipt identifiers into operator language: title case, acronyms, Account service, code lists, canary and authority vocabulary, backend prose untouched', () => {
    expect.soft(formatReceiptLabel('NO_LIVE_BINDING'), 'formats underscore, dot, dash, and uppercase receipt identifiers as title case: NO_LIVE_BINDING').toBe('No Live Binding');
    expect.soft(formatReceiptLabel('readiness'), 'formats underscore, dot, dash, and uppercase receipt identifiers as title case: readiness').toBe('Readiness');
    expect.soft(formatReceiptLabel('broker.connection'), 'formats underscore, dot, dash, and uppercase receipt identifiers as title case: broker.connection').toBe('Broker Connection');
    expect.soft(formatReceiptLabel('host-process.disabled_reason_code'), 'formats underscore, dot, dash, and uppercase receipt identifiers as title case: host-process.disabled_reason_code').toBe(
      'Host Process Disabled Reason Code',
    );
    expect.soft(formatReceiptLabel('FAILED'), 'formats underscore, dot, dash, and uppercase receipt identifiers as title case: FAILED').toBe('Failed');

    expect.soft(formatReceiptLabel('ibkr_api_evidence'), 'preserves known acronyms: ibkr_api_evidence').toBe('IBKR API Evidence');
    expect.soft(formatReceiptLabel('intent_wal_pnl'), 'preserves known acronyms: intent_wal_pnl').toBe('Intent WAL P&L');
    expect.soft(formatReceiptLabel('RTH'), 'preserves known acronyms: RTH').toBe('RTH');

    expect.soft(formatReceiptLabel('clerk'), 'renders the internal clerk event kind as Account service language: clerk').toBe('Account service');
    expect.soft(formatReceiptLabel('account_clerk'), 'renders the internal clerk event kind as Account service language: account_clerk').toBe('Account service');

    expect.soft(formatReceiptLabel('COMMAND_LOOP_STALE, CONTROL_PLANE_LEASE_STALE'), 'formats comma-separated code lists').toBe(
      'Command Loop Stale, Control Plane Lease Stale',
    );

    expect.soft(formatReceiptLabel('CANARY_PAIRING_NOT_ALLOWLISTED'), 'renders the governed Signal Program canary vocabulary as operator language: CANARY_PAIRING_NOT_ALLOWLISTED').toBe(
      'Canary Pairing Not Allowlisted',
    );
    expect.soft(formatReceiptLabel('CANARY_ROLLBACK_REQUIRES_FLATTEN'), 'renders the governed Signal Program canary vocabulary as operator language: CANARY_ROLLBACK_REQUIRES_FLATTEN').toBe(
      'Canary Rollback Requires Flatten',
    );
    expect.soft(formatReceiptLabel('CANARY_ROLLBACK_BOUNDARY_UNPROVABLE'), 'renders the governed Signal Program canary vocabulary as operator language: CANARY_ROLLBACK_BOUNDARY_UNPROVABLE').toBe(
      'Canary Rollback Boundary Unprovable',
    );
    expect.soft(formatReceiptLabel('PROGRAM_BUILD_UNPROVEN'), 'renders the governed Signal Program canary vocabulary as operator language: PROGRAM_BUILD_UNPROVEN').toBe('Program Build Unproven');
    expect.soft(formatReceiptLabel('CANDIDATE_UNCAPTURED_AT_CRASH'), 'renders the governed Signal Program canary vocabulary as operator language: CANDIDATE_UNCAPTURED_AT_CRASH').toBe(
      'Candidate Uncaptured At Crash',
    );

    expect.soft(formatReceiptLabel('real_paper'), 'renders authority, provenance, and verification vocabulary: real_paper').toBe('Real Paper');
    expect.soft(formatReceiptLabel('synthetic'), 'renders authority, provenance, and verification vocabulary: synthetic').toBe('Synthetic');
    expect.soft(formatReceiptLabel('clerk_decision_receipt'), 'renders authority, provenance, and verification vocabulary: clerk_decision_receipt').toBe('Clerk Decision Receipt');
    expect.soft(formatReceiptLabel('legacy_simulated_wal'), 'renders authority, provenance, and verification vocabulary: legacy_simulated_wal').toBe('Legacy Simulated WAL');
    expect.soft(formatReceiptLabel('live_reproof'), 'renders authority, provenance, and verification vocabulary: live_reproof').toBe('Live Reproof');
    expect.soft(formatReceiptLabel('frozen_run_evidence'), 'renders authority, provenance, and verification vocabulary: frozen_run_evidence').toBe('Frozen Run Evidence');

    expect.soft(formatReceiptLabel('Broker snapshot disagrees with the intent WAL.'), 'leaves backend prose untouched').toBe(
      'Broker snapshot disagrees with the intent WAL.',
    );
  });

  it('formats receipt values by their label: opaque audit tokens and digests preserved, code-like values formatted', () => {
    expect.soft(formatReceiptValue('intent_id', 'intent-7'), 'preserves opaque audit token values based on the receipt label: intent_id').toBe('intent-7');
    expect.soft(formatReceiptValue('intent-id', 'intent-7'), 'preserves opaque audit token values based on the receipt label: intent-id').toBe('intent-7');
    expect.soft(formatReceiptValue('runbook.url', 'https://example.test/runbook?id=7'), 'preserves opaque audit token values based on the receipt label: runbook.url').toBe(
      'https://example.test/runbook?id=7',
    );

    expect.soft(formatReceiptValue('lean_image_digest', 'bdb7c7aa3bd5f19690544270'), 'preserves a content-addressed digest instead of title-casing its hex: lean_image_digest').toBe(
      'bdb7c7aa3bd5f19690544270',
    );
    expect.soft(formatReceiptValue('content_digest', 'sha256:3dd003372f1ef1981b4e'), 'preserves a content-addressed digest instead of title-casing its hex: content_digest').toBe(
      'sha256:3dd003372f1ef1981b4e',
    );
    expect.soft(formatReceiptValue('fixture_sha256', 'a4f729d1e8c0'), 'preserves a content-addressed digest instead of title-casing its hex: fixture_sha256').toBe('a4f729d1e8c0');

    expect.soft(formatReceiptValue('state', 'NO_LIVE_BINDING'), 'formats code-like receipt values when the label is not opaque: state').toBe('No Live Binding');
    expect.soft(formatReceiptValue('source', 'readiness'), 'formats code-like receipt values when the label is not opaque: source').toBe('Readiness');
    expect.soft(formatReceiptValue('attempt_count', 3), 'formats code-like receipt values when the label is not opaque: attempt_count').toBe('3');
    expect.soft(formatReceiptValue('available', true), 'formats code-like receipt values when the label is not opaque: available').toBe('True');
  });

});
