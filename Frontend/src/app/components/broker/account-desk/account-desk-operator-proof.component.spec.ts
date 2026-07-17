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

    expect(await screen.findByRole('heading', { name: 'Current account checks' })).toBeTruthy();
    expect(screen.getByText('No reconciliation receipt has been recorded.')).toBeTruthy();
    expect(screen.getByText('No prior observation events are available.')).toBeTruthy();
    expect(screen.getByText('Account verification is not available yet.')).toBeTruthy();
  });

  it('collapses older verification updates behind an explicit disclosure', async () => {
    const triage = makeCleanAccountTriage({
      accountObservation: {
        state: 'REVOKED',
        reason_line: 'Live broker positions cannot be proven.',
        observed_at_ms: 1_780_000_000_000,
        valid_until_ms: null,
        history: [
          { state: 'REVOKED', reason_line: 'Newest update.', recorded_at_ms: 1_780_000_000_000 },
          { state: 'VERIFIED', reason_line: 'Previous update.', recorded_at_ms: 1_779_999_999_000 },
          { state: 'REVOKED', reason_line: 'Earlier update.', recorded_at_ms: 1_779_999_998_000 },
          { state: 'VERIFIED', reason_line: 'Oldest update.', recorded_at_ms: 1_779_999_997_000 },
        ],
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

    expect(await screen.findByRole('heading', { name: 'Account verification' })).toBeTruthy();
    expect(screen.getByText('Show 1 earlier verification update')).toBeTruthy();
  });
});
