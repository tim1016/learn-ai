import { signal } from '@angular/core';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { FleetAccountSummary } from '../../../api/live-instances.types';
import { AccountDeskFleetStore } from './account-desk-fleet-store.service';
import { AccountDeskOperatorFleetComponent } from './account-desk-operator-fleet.component';

describe('AccountDeskOperatorFleetComponent', () => {
  it('renders server-owned fleet contamination with the explicit no-position state', async () => {
    await render(AccountDeskOperatorFleetComponent, {
      providers: [{
        provide: AccountDeskFleetStore,
        useValue: {
          summary: signal(summary()),
          loading: signal(false),
          errorMessage: signal(null),
          hasLastGood: signal(true),
          showingStaleLastGood: signal(false),
          lastGoodAtMs: signal(1_780_000_000_000),
          retry: vi.fn(),
        },
      }],
    });

    expect(await screen.findByRole('heading', { name: 'Cross-bot position check' })).toBeTruthy();
    expect(screen.getByText('Fleet is clean.')).toBeTruthy();
    expect(screen.getByText('No other bot positions are reported.')).toBeTruthy();
  });
});

function summary(): FleetAccountSummary {
  return {
    account_id: 'DU1234567',
    account_identity: 'CONSISTENT',
    account_identity_reason_codes: [],
    contamination: {
      net_positions: {},
      explained_total: {},
      explained_by_instance: [],
      residual: {},
      verdict: 'clean',
      policy_blocks_starts: false,
      summary: 'Fleet is clean.',
    },
  };
}
