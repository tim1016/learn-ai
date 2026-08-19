import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { EvidenceEntry, EvidencePage } from '../lib/broker-v2-panel.types';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import { TransactionEvidenceTimelineComponent } from './transaction-evidence-timeline.component';

interface Deferred<T> {
  readonly promise: Promise<T>;
  readonly resolve: (value: T) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function entry(seq: number, summary: string): EvidenceEntry {
  return {
    seq,
    kind: 'ORDER_EVENT',
    kind_label: `Order event ${seq}`,
    recorded_at_ms: 1_700_000_000_000 + seq,
    order_ref: 'tx-1',
    intent_id: `intent-${seq}`,
    summary,
    has_more_detail: false,
  };
}

function page(
  transactionRef: string,
  entries: EvidenceEntry[],
  nextCursor: string | number | null = null,
  totalEntries = nextCursor === null ? entries.length : entries.length + 1,
): EvidencePage {
  return {
    strategy_instance_id: 'sid-1',
    account_id: 'acc-1',
    transaction_ref: transactionRef,
    entries,
    next_cursor: nextCursor,
    total_entries: totalEntries,
    truncated: false,
    read_by: 'operator:test',
    read_at_ms: 1_700_000_001_000,
  };
}

function inputs(transactionRef = 'tx-1') {
  return {
    broker: 'alpaca',
    accountId: 'acc-1',
    sid: 'sid-1',
    transactionRef,
  };
}

describe('TransactionEvidenceTimelineComponent', () => {
  it('appends the next page once and guards duplicate load-more requests', async () => {
    const nextPage = deferred<EvidencePage>();
    const getEvidence = vi.fn()
      .mockResolvedValueOnce(page('tx-1', [entry(2, 'Newest event')], 1))
      .mockReturnValueOnce(nextPage.promise);
    const { fixture } = await render(TransactionEvidenceTimelineComponent, {
      inputs: inputs(),
      providers: [{ provide: BrokerV2PanelService, useValue: { getEvidence } }],
    });

    expect(await screen.findByText('Newest event')).toBeTruthy();
    const loadMore = screen.getByRole('button', { name: 'Load more events' });
    fireEvent.click(loadMore);
    fireEvent.click(loadMore);
    expect(getEvidence).toHaveBeenCalledTimes(2);
    expect(getEvidence).toHaveBeenLastCalledWith(
      'alpaca',
      'acc-1',
      'sid-1',
      expect.objectContaining({ cursor: 1, transactionRef: 'tx-1' }),
    );

    nextPage.resolve(page('tx-1', [entry(1, 'Older event')], null, 2));
    await fixture.whenStable();
    expect(await screen.findByText('Older event')).toBeTruthy();
    expect(screen.getByText('Newest event')).toBeTruthy();
  });

  it('discards a stale response after the transaction input changes', async () => {
    const first = deferred<EvidencePage>();
    const second = deferred<EvidencePage>();
    const getEvidence = vi.fn()
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    const { fixture } = await render(TransactionEvidenceTimelineComponent, {
      inputs: inputs('tx-a'),
      providers: [{ provide: BrokerV2PanelService, useValue: { getEvidence } }],
    });

    fixture.componentRef.setInput('transactionRef', 'tx-b');
    fixture.detectChanges();
    second.resolve(page('tx-b', [entry(2, 'Current transaction event')]));
    expect(await screen.findByText('Current transaction event')).toBeTruthy();

    first.resolve(page('tx-a', [entry(1, 'Stale transaction event')]));
    await fixture.whenStable();
    fixture.detectChanges();
    expect(screen.queryByText('Stale transaction event')).toBeNull();
    expect(screen.getByText('Current transaction event')).toBeTruthy();
  });

  it('recovers from an initial failure when the operator retries', async () => {
    const getEvidence = vi.fn()
      .mockRejectedValueOnce(new Error('Evidence endpoint unavailable'))
      .mockResolvedValueOnce(page('tx-1', [entry(1, 'Recovered event')]));
    const { fixture } = await render(TransactionEvidenceTimelineComponent, {
      inputs: inputs(),
      providers: [{ provide: BrokerV2PanelService, useValue: { getEvidence } }],
    });

    expect((await screen.findByRole('alert')).textContent).toContain('Evidence endpoint unavailable');
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await fixture.whenStable();

    expect(await screen.findByText('Recovered event')).toBeTruthy();
    expect(getEvidence).toHaveBeenCalledTimes(2);
  });

  it('renders source, observation, and record clocks from SQLite custody evidence', async () => {
    const sqliteEntry: EvidenceEntry = {
      ...entry(7, 'Order fill observed.'),
      operation_ref: 'effect:spy-bot:decision-7',
      custody_owner: 'Account clerk',
      source_event_at_ms: 1_700_000_000_007,
      clerk_observed_at_ms: 1_700_000_000_017,
      recorded_at_ms: 1_700_000_000_027,
    };
    await render(TransactionEvidenceTimelineComponent, {
      inputs: inputs(),
      providers: [
        {
          provide: BrokerV2PanelService,
          useValue: { getEvidence: vi.fn().mockResolvedValue(page('tx-1', [sqliteEntry])) },
        },
      ],
    });

    expect(await screen.findByText('Source event')).toBeTruthy();
    expect(screen.getByText('Clerk observed')).toBeTruthy();
    expect(screen.getByText('effect:spy-bot:decision-7')).toBeTruthy();
    expect(screen.getByText('Account clerk')).toBeTruthy();
    expect(screen.getAllByText(/2023/)).toHaveLength(3);
  });

  it('renders a code-like custody_owner through receiptLabel, not raw', async () => {
    const sqliteEntry: EvidenceEntry = {
      ...entry(8, 'Order accepted.'),
      custody_owner: 'ACCOUNT_CLERK',
    };
    await render(TransactionEvidenceTimelineComponent, {
      inputs: inputs(),
      providers: [
        {
          provide: BrokerV2PanelService,
          useValue: { getEvidence: vi.fn().mockResolvedValue(page('tx-1', [sqliteEntry])) },
        },
      ],
    });

    expect(await screen.findByText('Account service')).toBeTruthy();
    expect(screen.queryByText('ACCOUNT_CLERK')).toBeNull();
  });
});
