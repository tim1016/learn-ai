import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { CustodyDivergence } from '../../../api/alpaca.types';
import { CustodyDivergenceComponent } from './custody-divergence.component';

function divergence(overrides: Partial<CustodyDivergence> = {}): CustodyDivergence {
  return {
    kind: 'exposure_attribution_mismatch',
    state: 'resolvable_now',
    explanation: 'The broker holds exposure the Clerk cannot map.',
    possible_causes: [],
    position_deltas: [],
    resolution_step: null,
    prerequisite_detail: null,
    evidence_refs: [],
    ...overrides,
  } as CustodyDivergence;
}

describe('CustodyDivergenceComponent', () => {
  it('renders the kind/state through receiptLabel and the explanation verbatim', async () => {
    await render(CustodyDivergenceComponent, {
      inputs: { divergence: divergence({ state: 'needs_review' }) },
    });

    expect(screen.getByText('Exposure Attribution Mismatch')).toBeTruthy();
    expect(screen.getByText('Needs Review')).toBeTruthy();
    expect(
      screen.getByText('The broker holds exposure the Clerk cannot map.'),
    ).toBeTruthy();
  });

  it('renders the position-delta table for a divergence carrying deltas', async () => {
    await render(CustodyDivergenceComponent, {
      inputs: {
        divergence: divergence({
          position_deltas: [{ symbol: 'SPY', clerk_attributed_qty: 2, broker_observed_qty: 1 }],
        }),
      },
    });

    expect(screen.getByText('SPY')).toBeTruthy();
    expect(screen.getByRole('columnheader', { name: 'Clerk attributes' })).toBeTruthy();
    expect(screen.getByRole('columnheader', { name: 'Broker holds' })).toBeTruthy();
  });

  it('renders possible causes in a details/summary block', async () => {
    await render(CustodyDivergenceComponent, {
      inputs: {
        divergence: divergence({
          possible_causes: ['A bot process was terminated mid-run.'],
        }),
      },
    });

    expect(screen.getByText('Possible causes')).toBeTruthy();
    expect(screen.getByText('A bot process was terminated mid-run.')).toBeTruthy();
  });

  it('renders the prerequisite detail as a status paragraph', async () => {
    await render(CustodyDivergenceComponent, {
      inputs: {
        divergence: divergence({
          prerequisite_detail: 'Waiting on the broker to settle the corporate action.',
        }),
      },
    });

    expect(screen.getByRole('status').textContent).toBe(
      'Waiting on the broker to settle the corporate action.',
    );
  });

  it('renders evidence refs verbatim and unpiped, inside <code>', async () => {
    await render(CustodyDivergenceComponent, {
      inputs: {
        divergence: divergence({ evidence_refs: ['order-ref-abc123'] }),
      },
    });

    const list = screen.getByRole('list', { name: 'Evidence references' });
    const code = list.querySelector('code');
    expect(code?.textContent).toBe('order-ref-abc123');
  });

  it('omits the delta table, causes, prerequisite, and evidence sections when absent', async () => {
    await render(CustodyDivergenceComponent, { inputs: { divergence: divergence() } });

    expect(screen.queryByRole('table')).toBeNull();
    expect(screen.queryByText('Possible causes')).toBeNull();
    expect(screen.queryByRole('list', { name: 'Evidence references' })).toBeNull();
    expect(
      screen.queryByText('Waiting on the broker to settle the corporate action.'),
    ).toBeNull();
  });
});
