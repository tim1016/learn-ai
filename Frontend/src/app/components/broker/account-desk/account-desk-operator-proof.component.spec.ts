import { signal } from '@angular/core';
import { Router } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { makeCleanAccountTriage } from '../testing/account-triage-fixtures';
import { AccountDeskGuidanceStore } from './account-desk-guidance-store.service';
import { AccountDeskOperatorProofComponent } from './account-desk-operator-proof.component';
import { AccountDeskSurfaceStore } from './account-desk-surface-store.service';

describe('AccountDeskOperatorProofComponent', () => {
  it('renders server evidence and honest explicit receipt/history empty states', async () => {
    const triage = makeCleanAccountTriage({
      accountObservation: {
        state: 'ABSENT',
        reason_line: 'Account verification is not available yet.',
        observed_at_ms: null,
        valid_until_ms: null,
        history: [],
      },
    });
    await render(AccountDeskOperatorProofComponent, {
      providers: [
        {
          provide: AccountDeskSurfaceStore,
          useValue: {
            triage: signal(triage),
            loading: signal(false),
            error: signal(null),
            showingStaleLastGood: signal(false),
            retry: vi.fn(),
          },
        },
        { provide: AccountDeskGuidanceStore, useValue: { blockersFor: vi.fn().mockReturnValue([]) } },
        { provide: Router, useValue: { navigate: vi.fn() } },
      ],
    });

    expect(await screen.findByRole('heading', { name: 'Operations proof' })).toBeTruthy();
    expect(screen.getByText('No reconciliation receipt has been recorded.')).toBeTruthy();
    expect(screen.getByText('No prior observation events are available.')).toBeTruthy();
    expect(screen.getByText('Account verification is not available yet.')).toBeTruthy();
  });
});
