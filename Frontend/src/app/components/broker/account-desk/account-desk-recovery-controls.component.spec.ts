import { signal } from '@angular/core';
import { Router } from '@angular/router';
import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { makeCleanAccountTriage } from '../testing/account-triage-fixtures';
import { AccountDeskGuidanceStore } from './account-desk-guidance-store.service';
import { AccountDeskRecoveryControlsComponent } from './account-desk-recovery-controls.component';
import { AccountDeskRecoveryStore } from './account-desk-recovery-store.service';
import { AccountDeskSurfaceStore } from './account-desk-surface-store.service';

describe('AccountDeskRecoveryControlsComponent', () => {
  it('keeps the Cure tools card honest when no server action is declared and confirms policy changes', async () => {
    const recovery = recoveryStore();
    const triage = makeCleanAccountTriage({
      automationPolicy: {
        schema_version: 1, account_id: 'DU1234567', enabled: false, updated_at_ms: 0, updated_by: 'system.default',
      },
    });
    await render(AccountDeskRecoveryControlsComponent, {
      providers: [
        { provide: AccountDeskSurfaceStore, useValue: { accountId: signal('DU1234567'), triage: signal(triage), loading: signal(false), error: signal(null), retry: vi.fn() } },
        { provide: AccountDeskGuidanceStore, useValue: { blockersFor: vi.fn().mockReturnValue([]) } },
        { provide: AccountDeskRecoveryStore, useValue: recovery },
        { provide: Router, useValue: { navigate: vi.fn() } },
      ],
    });

    expect(await screen.findByRole('heading', { name: 'Account recovery' })).toBeTruthy();
    expect(screen.getByText('No account recovery action is currently declared safe.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Enable auto-reconcile' }));
    expect(recovery.requestAutomationChange).toHaveBeenCalledWith(triage.reconciliation_automation_policy);
  });

  it('renders the returned reconciliation receipt without changing opaque tokens', async () => {
    const recovery = recoveryStore({
      success: signal({
        kind: 'reconcile',
        receipt: {
          receipt_id: 'receipt:opaque/1', generated_at_ms: 1_780_000_000_000,
          final_gate_result: { status: 'pass' },
        },
      }),
    });
    await render(AccountDeskRecoveryControlsComponent, {
      providers: [
        { provide: AccountDeskSurfaceStore, useValue: { accountId: signal('DU1234567'), triage: signal(makeCleanAccountTriage()), loading: signal(false), error: signal(null), retry: vi.fn() } },
        { provide: AccountDeskGuidanceStore, useValue: { blockersFor: vi.fn().mockReturnValue([]) } },
        { provide: AccountDeskRecoveryStore, useValue: recovery },
        { provide: Router, useValue: { navigate: vi.fn() } },
      ],
    });

    expect(await screen.findByText('receipt:opaque/1')).toBeTruthy();
  });

  it('renders a returned journal receipt with its opaque evidence token unchanged', async () => {
    const recovery = recoveryStore({
      success: signal({
        kind: 'journal_cure',
        receipt: {
          bot_order_namespace: 'learn-ai/retired-bot/v1', symbol: 'SPY', journal_seq: 9,
          evidence_refs: ['receipt:opaque/1'], recorded_at_ms: 1_780_000_000_000,
        },
      }),
    });
    await render(AccountDeskRecoveryControlsComponent, {
      providers: [
        { provide: AccountDeskSurfaceStore, useValue: { accountId: signal('DU1234567'), triage: signal(makeCleanAccountTriage()), loading: signal(false), error: signal(null), retry: vi.fn() } },
        { provide: AccountDeskGuidanceStore, useValue: { blockersFor: vi.fn().mockReturnValue([]) } },
        { provide: AccountDeskRecoveryStore, useValue: recovery },
        { provide: Router, useValue: { navigate: vi.fn() } },
      ],
    });

    expect(await screen.findByText('receipt:opaque/1')).toBeTruthy();
  });
});

function recoveryStore(overrides: Record<string, unknown> = {}) {
  return {
    requestAutomationChange: vi.fn(),
    requestJournalCure: vi.fn(),
    requestLegacyRetirement: vi.fn(),
    refreshLegacyCandidates: vi.fn(),
    cancelConfirmation: vi.fn(),
    confirm: vi.fn(),
    setExposureOverrideReason: vi.fn(),
    confirmation: signal(null),
    busy: signal(false),
    errorMessage: signal<string | null>(null),
    success: signal(null),
    legacyCandidates: signal([]),
    legacyLoading: signal(false),
    legacyErrorMessage: signal<string | null>(null),
    ...overrides,
  };
}
