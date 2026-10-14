import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { EvidenceEntry, EvidencePage } from '../lib/broker-v2-panel.types';
import { JournalTailComponent } from './journal-tail.component';

function entry(seq: number): EvidenceEntry {
  return {
    seq,
    kind: 'ORDER_EVENT',
    kind_label: `Order event ${seq}`,
    recorded_at_ms: 1_700_000_000_000 + seq,
    order_ref: 'tx-1',
    intent_id: `intent-${seq}`,
    summary: `Event ${seq}`,
    has_more_detail: false,
  };
}

function page(entries: EvidenceEntry[]): EvidencePage {
  return {
    strategy_instance_id: 'sid-1',
    account_id: 'acc-1',
    transaction_ref: null,
    entries,
    next_cursor: null,
    total_entries: entries.length,
    truncated: false,
    read_by: 'operator:test',
    read_at_ms: 1_700_000_001_000,
  };
}

describe('JournalTailComponent', () => {
  it('says there are no journal entries for a page that read and held none', async () => {
    await render(JournalTailComponent, { inputs: { evidencePage: page([]) } });

    expect(screen.getByText('No journal entries.')).toBeTruthy();
    expect(screen.getByText('0 of 0 events')).toBeTruthy();
  });

  it('says neither "0 of 0 events" nor "No journal entries" when the read failed', async () => {
    await render(JournalTailComponent, { inputs: { evidencePage: null, failed: true } });

    expect(screen.queryByText('No journal entries.')).toBeNull();
    expect(screen.queryByText(/of 0 events/)).toBeNull();
  });

  it('lists the entries it was given', async () => {
    await render(JournalTailComponent, { inputs: { evidencePage: page([entry(2), entry(1)]) } });

    expect(screen.getByText('2 of 2 events')).toBeTruthy();
    expect(screen.getByText('Event 2')).toBeTruthy();
  });
});
