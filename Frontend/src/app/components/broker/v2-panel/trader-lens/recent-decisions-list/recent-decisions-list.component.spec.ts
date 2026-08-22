import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { RecentDecisionsListComponent } from './recent-decisions-list.component';
import type { RecentDecisionView } from '../../lib/broker-v2-panel.types';

const SYNTHETIC_DECISION: RecentDecisionView = {
  seq: 5,
  recorded_at_ms: 1_753_800_000_000,
  outcome: 'entered',
  reason_code: 'CROSS_UP',
  bar_ref: 'SPY@1753799999900',
  order_ref: 'simulated:run-dry:1753799999900:ENTER',
  simulated: true,
  authority_account_id: 'sim:bot-alpha',
  authority_kind: 'synthetic',
};

describe('RecentDecisionsListComponent', () => {
  it('renders each decision with its authority visibly distinguishable (issue #1729 AC #8)', async () => {
    await render(RecentDecisionsListComponent, {
      inputs: { decisions: [SYNTHETIC_DECISION] },
    });

    expect(screen.getByRole('table', { name: 'Recent decisions' })).toBeTruthy();
    expect(screen.getByText('Entered')).toBeTruthy();
    expect(screen.getByText('Cross Up')).toBeTruthy();
    expect(screen.getByText('Synthetic')).toBeTruthy();
  });

  it('renders the honest empty state when there is no simulated activity yet', async () => {
    await render(RecentDecisionsListComponent, {
      inputs: { decisions: [] },
    });

    expect(screen.getByText('No simulated decisions recorded yet.')).toBeTruthy();
    expect(screen.queryByRole('table', { name: 'Recent decisions' })).toBeNull();
  });
});
