import { signal } from '@angular/core';
import { Router } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { AccountServiceStatusResponse } from '../../../api/account-directory.types';
import { formatTimestampDisplay } from '../../../shared/timestamp';
import { AccountDeskDirectoryStore } from './account-desk-directory-store.service';
import { AccountDeskGuidanceStore } from './account-desk-guidance-store.service';
import { AccountDeskOperatorServiceComponent } from './account-desk-operator-service.component';

describe('AccountDeskOperatorServiceComponent', () => {
  it('renders Account service evidence in viewer-local time without exposing the internal implementation name', async () => {
    const directory = {
      serviceStatus: signal(status()),
      serviceStatusLoading: signal(false),
      serviceStatusErrorMessage: signal<string | null>(null),
      serviceStatusHasLastGood: signal(true),
      serviceStatusShowingStaleLastGood: signal(false),
      retryServiceStatus: vi.fn(),
    };
    await render(AccountDeskOperatorServiceComponent, {
      providers: [
        { provide: AccountDeskDirectoryStore, useValue: directory },
        { provide: AccountDeskGuidanceStore, useValue: { blockersFor: vi.fn().mockReturnValue([]) } },
        { provide: Router, useValue: { navigate: vi.fn() } },
      ],
    });

    expect(screen.getByRole('heading', { name: 'Account service' })).toBeTruthy();
    expect(screen.getAllByText(formatTimestampDisplay(1_780_000_000_102, { mode: 'local' })).length).toBeGreaterThan(0);
    expect(screen.queryByText(/clerk/i)).toBeNull();
  });
});

function status(): AccountServiceStatusResponse {
  return {
    schema_version: 1,
    account_id: 'DU1234567',
    attachment: 'ATTACHED',
    phase: 'accepting',
    generation: 2,
    generation_recorded_at_ms: 1_780_000_000_100,
    source: 'host_daemon.clerk_spawn',
    binding: { state: 'ATTACHED', generation: 2, lease_generation: 2 },
    lease: {
      status: 'RUNNING',
      generation: 2,
      started_at_ms: 1_780_000_000_101,
      renewed_at_ms: 1_780_000_000_102,
      valid_until_ms: 1_780_000_060_102,
    },
    journal: { last_seq: 9, last_write_ms: 1_780_000_000_103 },
  };
}
