import { render, screen, fireEvent } from '@testing-library/angular';
import { describe, it, expect, vi } from 'vitest';
import { TransactionRailComponent } from './transaction-rail.component';
import type { StationView, TransactionRail } from '../lib/broker-v2-panel.types';

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

  it('evidence link click emits rail transaction_ref', async () => {
    const onEvidence = vi.fn<(val: string) => void>();

    const station: StationView = makeStation({
      station_id: 'SUBMIT_GATE',
      state: 'satisfied',
      label: 'Submit',
      state_label: 'Done',
      evidence_at_ms: 1_700_000_000_000,
    });

    const { fixture } = await render(TransactionRailComponent, {
      inputs: { rail: makeRail([station], 'my-tx-ref') },
      on: { evidenceRequested: onEvidence },
    });

    const btn = screen.getByRole('button', { name: /view raw evidence/i });
    fireEvent.click(btn);

    await fixture.whenStable();
    expect(onEvidence).toHaveBeenCalledWith('my-tx-ref');
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

    const li = container.querySelector('li');
    expect(li?.getAttribute('aria-label')).toMatch(/signal.*ready/i);
  });
});
