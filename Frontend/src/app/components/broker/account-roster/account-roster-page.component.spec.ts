import { signal } from '@angular/core';
import { Router } from '@angular/router';
import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { AccountRosterRow } from '../../../api/account-directory.types';
import { formatTimestampDisplay } from '../../../shared/timestamp';
import { AccountDeskDirectoryStore } from '../account-desk/account-desk-directory-store.service';
import { AccountRosterPageComponent } from './account-roster-page.component';

function makeDirectory(rows: readonly AccountRosterRow[] = []) {
  return {
    rosterRows: signal(rows),
    rosterLoading: signal(false),
    rosterErrorMessage: signal<string | null>(null),
    rosterHasLastGood: signal(rows.length > 0),
    rosterShowingStaleLastGood: signal(false),
    rosterEmpty: signal(rows.length === 0),
    loadRoster: vi.fn().mockResolvedValue(undefined),
    retryRoster: vi.fn(),
  };
}

async function setup(directory = makeDirectory([row()])) {
  const router = { navigate: vi.fn().mockResolvedValue(true) };
  await render(AccountRosterPageComponent, {
    providers: [
      { provide: AccountDeskDirectoryStore, useValue: directory },
      { provide: Router, useValue: router },
    ],
  });
  return { directory, router };
}

describe('AccountRosterPageComponent', () => {
  it('renders backend-owned roster facts in local time and opens the selected account desk', async () => {
    const { router } = await setup();

    expect(screen.getByText(/Verification is required/)).toBeTruthy();
    expect(screen.getByText(formatTimestampDisplay(1_780_000_000_000, { mode: 'local' }))).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Open account DU1234567' }));

    expect(router.navigate).toHaveBeenCalledWith(['/broker/accounts', 'DU1234567']);
  });

  it('renders the configured-empty state', async () => {
    await setup(makeDirectory());

    expect(screen.getByText('No configured accounts are available.')).toBeTruthy();
  });

  it('renders a loading state before the first roster response', async () => {
    const loading = makeDirectory();
    loading.rosterLoading.set(true);
    await setup(loading);

    expect(screen.getByText('Loading accounts…')).toBeTruthy();
  });

  it('renders a retryable initial error', async () => {
    const failed = makeDirectory();
    failed.rosterErrorMessage.set('Account roster is unavailable. Retry to request it again.');
    await setup(failed);
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(failed.retryRoster).toHaveBeenCalledOnce();
  });

  it('renders a stale last-good warning separately', async () => {
    const stale = makeDirectory([row()]);
    stale.rosterShowingStaleLastGood.set(true);
    await setup(stale);
    expect(screen.getByText('Showing the last available account roster. Refresh is unavailable.')).toBeTruthy();
  });
});

function row(): AccountRosterRow {
  return {
    account_id: 'DU1234567',
    broker: 'IBKR',
    effective_posture: 'PAPER_EXECUTION',
    service: { attachment: 'ATTACHED', phase: 'accepting', generation: 3 },
    latest_verdict_summary: {
      state: 'NOT_PROVEN',
      headline: 'Verification is required.',
      generated_at_ms: 1_780_000_000_000,
    },
    last_verified_at_ms: 1_780_000_000_000,
  };
}
