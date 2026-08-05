import { render, screen, fireEvent } from '@testing-library/angular';
import { describe, it, expect, vi } from 'vitest';
import { TransactionRailComponent } from './transaction-rail.component';
import type { EvidencePage, StationView, TransactionRail } from '../lib/broker-v2-panel.types';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';

function makeStation(
  overrides: Partial<StationView> & Pick<StationView, 'station_id' | 'state'>,
): StationView {
  return {
    label: overrides.label ?? 'Station',
    state_label: overrides.state_label ?? overrides.state,
    receipt: '',
    evidence_at_ms: null,
    blocker: null,
    ...overrides,
  };
}

function makeRail(stations: StationView[], transactionRef = 'tx-001'): TransactionRail {
  return { transaction_ref: transactionRef, stations };
}

describe('TransactionRailComponent', () => {
  it('renders all five station states with icon AND text (not color-only)', async () => {
    const stations: StationView[] = [
      makeStation({ station_id: 'SIGNAL', state: 'satisfied', label: 'Signal', state_label: 'Ready' }),
      makeStation({ station_id: 'INTENT', state: 'waiting', label: 'Lock', state_label: 'Pending' }),
      makeStation({ station_id: 'SUBMIT_GATE', state: 'blocked', label: 'Submit', state_label: 'Blocked' }),
      makeStation({ station_id: 'BROKER_ACK', state: 'unknown_stale', label: 'Fill', state_label: 'Unknown' }),
      makeStation({ station_id: 'FILL', state: 'not_applicable', label: 'Fee', state_label: 'N/A' }),
    ];

    await render(TransactionRailComponent, {
      inputs: { rail: makeRail(stations) },
    });

    // Each station must have icon + text both present
    expect(screen.getByText('✓')).toBeTruthy();  // satisfied icon
    expect(screen.getByText('⏳')).toBeTruthy(); // waiting icon
    expect(screen.getByText('⚠')).toBeTruthy(); // blocked icon

    // Text labels
    expect(screen.getByText('Signal')).toBeTruthy();
    expect(screen.getByText('Lock')).toBeTruthy();
    expect(screen.getByText('Submit')).toBeTruthy();

    // State labels
    expect(screen.getByText('Ready')).toBeTruthy();
    expect(screen.getByText('Pending')).toBeTruthy();
    expect(screen.getByText('Blocked')).toBeTruthy();
  });

  it('satisfied station has station--satisfied CSS class', async () => {
    const stations: StationView[] = [
      makeStation({ station_id: 'SIGNAL', state: 'satisfied', label: 'Signal', state_label: 'Ready' }),
    ];

    const { container } = await render(TransactionRailComponent, {
      inputs: { rail: makeRail(stations) },
    });

    const item = container.querySelector('.station--satisfied');
    expect(item).not.toBeNull();
  });

  it('blocked station has station--blocked CSS class', async () => {
    const stations: StationView[] = [
      makeStation({ station_id: 'SUBMIT_GATE', state: 'blocked', label: 'Submit', state_label: 'Blocked' }),
    ];

    const { container } = await render(TransactionRailComponent, {
      inputs: { rail: makeRail(stations) },
    });

    const item = container.querySelector('.station--blocked');
    expect(item).not.toBeNull();
  });

  it('opens attention stations and keeps satisfied stations collapsed', async () => {
    const stations: StationView[] = [
      makeStation({ station_id: 'SIGNAL', state: 'waiting', label: 'Signal', state_label: 'Waiting' }),
      makeStation({ station_id: 'INTENT', state: 'satisfied', label: 'Intent', state_label: 'Ready' }),
    ];

    const { container } = await render(TransactionRailComponent, {
      inputs: { rail: makeRail(stations) },
    });

    expect(container.querySelector('.station--waiting details')?.hasAttribute('open')).toBe(true);
    expect(container.querySelector('.station--satisfied details')?.hasAttribute('open')).toBe(false);
  });

  it('loads and renders transaction evidence inside the station accordion', async () => {
    const station: StationView = makeStation({
      station_id: 'SUBMIT_GATE',
      state: 'satisfied',
      label: 'Submit',
      state_label: 'Done',
      evidence_at_ms: 1_700_000_000_000,
    });

    const evidence: EvidencePage = {
      strategy_instance_id: 'sid-1',
      account_id: 'acc-1',
      transaction_ref: 'my-tx-ref',
      entries: [
        {
          seq: 7,
          kind: 'ORDER_FILLED',
          kind_label: 'Order filled',
          recorded_at_ms: 1_700_000_000_000,
          order_ref: 'my-tx-ref',
          intent_id: 'intent-7',
          summary: 'Filled 10 shares at the broker.',
          has_more_detail: false,
        },
        {
          seq: 6,
          kind: 'ORDER_EVENT',
          kind_label: 'Order event',
          recorded_at_ms: 1_699_999_999_000,
          order_ref: 'my-tx-ref',
          intent_id: 'intent-7',
          summary: 'kind=order_event | intent=intent-7 | event=fill | filled=10@100',
          has_more_detail: false,
        },
      ],
      next_cursor: null,
      total_entries: 2,
      truncated: false,
      read_by: 'operator:test',
      read_at_ms: 1_700_000_000_001,
    };
    const getEvidence = vi.fn().mockResolvedValue(evidence);

    const { fixture, container } = await render(TransactionRailComponent, {
      inputs: {
        rail: makeRail([station], 'my-tx-ref'),
        broker: 'alpaca',
        accountId: 'acc-1',
        sid: 'sid-1',
      },
      providers: [{ provide: BrokerV2PanelService, useValue: { getEvidence } }],
    });

    const btn = screen.getByRole('button', { name: /view raw evidence/i });
    fireEvent.click(btn);

    await fixture.whenStable();
    expect(await screen.findByText('Order filled')).toBeTruthy();
    expect(screen.getByText('Filled 10 shares at the broker.')).toBeTruthy();
    expect(screen.getByText(/kind=order_event/)).toBeTruthy();
    expect(container.querySelector('.evidence-drawer')).toBeNull();
    expect(getEvidence).toHaveBeenCalledWith(
      'alpaca',
      'acc-1',
      'sid-1',
      expect.objectContaining({ transactionRef: 'my-tx-ref' }),
    );
  });

  it('blocked station with OperatorBlocker shows headline text', async () => {
    const stations: StationView[] = [
      makeStation({
        station_id: 'SUBMIT_GATE',
        state: 'blocked',
        label: 'Submit',
        state_label: 'Blocked',
        blocker: {
          condition: {
            id: 'NO_LIVE_BINDING',
            severity: 'blocking',
            scope: 'bot',
          },
          host: 'bot_cockpit',
          anchor: { kind: 'surface', subject_key: null },
          audience: 'operator',
          disposition: 'wait',
          headline: 'No live binding',
          detail: 'The broker is not connected.',
          primary_move: null,
          secondary_moves: [],
          applies_to: 'run',
        },
      }),
    ];

    await render(TransactionRailComponent, {
      inputs: { rail: makeRail(stations) },
    });

    expect(screen.getByText('No live binding')).toBeTruthy();
  });

  it('not-applicable station uses — icon and N/A text', async () => {
    const stations: StationView[] = [
      makeStation({ station_id: 'FILL', state: 'not_applicable', label: 'Fee', state_label: 'N/A' }),
    ];

    await render(TransactionRailComponent, {
      inputs: { rail: makeRail(stations) },
    });

    expect(screen.getByText('—')).toBeTruthy();
    expect(screen.getByText('N/A')).toBeTruthy();
  });

  it('null transaction_ref shows no-transaction message', async () => {
    await render(TransactionRailComponent, {
      inputs: { rail: { transaction_ref: null, stations: [] } },
    });

    expect(screen.getByText(/no active transaction/i)).toBeTruthy();
  });

  it('each station list item has an aria-label for WCAG AA', async () => {
    const stations: StationView[] = [
      makeStation({ station_id: 'SIGNAL', state: 'satisfied', label: 'Signal', state_label: 'Ready' }),
    ];

    const { container } = await render(TransactionRailComponent, {
      inputs: { rail: makeRail(stations) },
    });

    const summary = container.querySelector('summary');
    expect(summary?.getAttribute('aria-label')).toMatch(/signal.*ready/i);
  });
});
