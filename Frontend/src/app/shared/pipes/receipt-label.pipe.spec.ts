import { describe, expect, it } from 'vitest';

import { formatReceiptLabel, ReceiptLabelPipe } from './receipt-label.pipe';

describe('formatReceiptLabel', () => {
  it('formats underscore, dot, dash, and uppercase receipt identifiers as title case', () => {
    expect(formatReceiptLabel('NO_LIVE_BINDING')).toBe('No Live Binding');
    expect(formatReceiptLabel('broker.connection')).toBe('Broker Connection');
    expect(formatReceiptLabel('host-process.disabled_reason_code')).toBe(
      'Host Process Disabled Reason Code',
    );
    expect(formatReceiptLabel('FAILED')).toBe('Failed');
  });

  it('preserves known acronyms', () => {
    expect(formatReceiptLabel('ibkr_api_evidence')).toBe('IBKR API Evidence');
    expect(formatReceiptLabel('intent_wal_pnl')).toBe('Intent WAL P&L');
  });

  it('formats comma-separated code lists', () => {
    expect(formatReceiptLabel('COMMAND_LOOP_STALE, CONTROL_PLANE_LEASE_STALE')).toBe(
      'Command Loop Stale, Control Plane Lease Stale',
    );
  });

  it('leaves backend prose untouched', () => {
    expect(formatReceiptLabel('Broker snapshot disagrees with the intent WAL.')).toBe(
      'Broker snapshot disagrees with the intent WAL.',
    );
  });

  it('supports the Angular pipe wrapper', () => {
    expect(new ReceiptLabelPipe().transform('already_running')).toBe('Already Running');
  });
});
