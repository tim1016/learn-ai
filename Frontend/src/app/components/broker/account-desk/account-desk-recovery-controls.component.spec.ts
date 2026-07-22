import { signal } from '@angular/core';
import { Router } from '@angular/router';
import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { formatTimestampDisplay } from '../../../shared/timestamp';
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
    expect(screen.getByText(formatTimestampDisplay(1_780_000_000_000, { mode: 'local' }))).toBeTruthy();
  });

  it('shows the backend-declared emergency flatten action', async () => {
    const recovery = recoveryStore();
    const confirmation = {
      title: 'Emergency flatten paper account', body: 'Backend body.', consequence: 'Backend consequence.',
      confirm_label: 'Emergency flatten account', required_token: 'FLATTEN',
    };
    await render(AccountDeskRecoveryControlsComponent, {
      providers: [
        { provide: AccountDeskSurfaceStore, useValue: { accountId: signal('DU1234567'), triage: signal(makeCleanAccountTriage({ emergencyFlattenConfirmation: confirmation })), loading: signal(false), error: signal(null), retry: vi.fn() } },
        { provide: AccountDeskGuidanceStore, useValue: { blockersFor: vi.fn().mockReturnValue([]) } },
        { provide: AccountDeskRecoveryStore, useValue: recovery },
        { provide: Router, useValue: { navigate: vi.fn() } },
      ],
    });

    fireEvent.click(await screen.findByRole('button', { name: 'Emergency flatten' }));
    expect(recovery.requestEmergencyFlatten).toHaveBeenCalledWith(confirmation);
    expect(screen.queryByText('No account recovery action is currently declared safe.')).toBeNull();
  });

  it('fails closed when an older backend omits emergency flatten confirmation', async () => {
    const recovery = recoveryStore();
    const olderTriageContract = makeCleanAccountTriage() as Partial<ReturnType<typeof makeCleanAccountTriage>>;
    delete olderTriageContract.emergency_flatten_confirmation;

    await render(AccountDeskRecoveryControlsComponent, {
      providers: [
        { provide: AccountDeskSurfaceStore, useValue: { accountId: signal('DU1234567'), triage: signal(olderTriageContract), loading: signal(false), error: signal(null), retry: vi.fn() } },
        { provide: AccountDeskGuidanceStore, useValue: { blockersFor: vi.fn().mockReturnValue([]) } },
        { provide: AccountDeskRecoveryStore, useValue: recovery },
        { provide: Router, useValue: { navigate: vi.fn() } },
      ],
    });

    expect(screen.queryByRole('button', { name: 'Emergency flatten' })).toBeNull();
    expect(await screen.findByText('No account recovery action is currently declared safe.')).toBeTruthy();
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
    expect(screen.getByText(formatTimestampDisplay(1_780_000_000_000, { mode: 'local' }))).toBeTruthy();
  });

  it.each([
    {
      name: 'automation',
      success: {
        kind: 'automation',
        policy: {
          enabled: true, updated_at_ms: 1_780_000_000_000, updated_by: 'account-desk.operator',
        },
      },
      values: ['Enabled', 'account-desk.operator', formatTimestampDisplay(1_780_000_000_000, { mode: 'local' })],
    },
    {
      name: 'freeze clear',
      success: {
        kind: 'clear_freeze',
        receipt: {
          recovery_id: 'recovery:opaque/1', receipt_id: 'receipt:opaque/2', gate_result: { status: 'pass' },
        },
      },
      values: ['recovery:opaque/1', 'receipt:opaque/2', 'Pass'],
    },
    {
      name: 'exposure override',
      success: {
        kind: 'exposure_override',
        receipt: { override_id: 'override:opaque/1', account_id: 'DU1234567' },
      },
      values: ['override:opaque/1', 'DU1234567'],
    },
    {
      name: 'legacy retirement',
      success: {
        kind: 'legacy_retire',
        receipt: {
          receipt_id: 'receipt:opaque/3', strategy_instance_id: 'strategy:opaque/1', run_id: 'run:opaque/1',
          bot_order_namespace: 'learn-ai/retired-bot/v1', retired_at_ms: 1_780_000_000_000,
        },
      },
      values: ['receipt:opaque/3', 'strategy:opaque/1', 'run:opaque/1', 'learn-ai/retired-bot/v1', formatTimestampDisplay(1_780_000_000_000, { mode: 'local' })],
    },
    {
      name: 'recovery flatten',
      success: {
        kind: 'recovery_flatten',
        receipt: {
          recovery_flatten: {
            recorded: { intent_id: 'intent:opaque/1', order_ref: 'order-ref:opaque/1' },
            broker_acked: { order_id: 7, recorded_at_ms: 1_780_000_000_000 },
          },
        },
      },
      values: ['intent:opaque/1', 'order-ref:opaque/1', '7', formatTimestampDisplay(1_780_000_000_000, { mode: 'local' })],
    },
    {
      name: 'journal recovery',
      success: {
        kind: 'journal_recovery',
        receipt: {
          receipt_id: 'journal-recovery-quarantine:opaque/1', account_id: 'DU1234567',
          phase: 'REBASELINE_REQUIRED', recorded_at_ms: 1_780_000_000_000,
          quarantined_journal_name: 'clerk_journal.jsonl.corrupt-opaque', broker_evidence_positions: [],
        },
      },
      values: [
        'journal-recovery-quarantine:opaque/1',
        'clerk_journal.jsonl.corrupt-opaque',
        'Rebaseline Required',
        formatTimestampDisplay(1_780_000_000_000, { mode: 'local' }),
      ],
    },
  ])('renders every displayed field for a returned $name receipt', async ({ success, values }) => {
    await renderRecoveryReceipt(success);

    for (const value of values) {
      expect(await screen.findByText(value)).toBeTruthy();
    }
  });
});

async function renderRecoveryReceipt(success: unknown) {
  const recovery = recoveryStore({ success: signal(success) });
  await render(AccountDeskRecoveryControlsComponent, {
    providers: [
      { provide: AccountDeskSurfaceStore, useValue: { accountId: signal('DU1234567'), triage: signal(makeCleanAccountTriage()), loading: signal(false), error: signal(null), retry: vi.fn() } },
      { provide: AccountDeskGuidanceStore, useValue: { blockersFor: vi.fn().mockReturnValue([]) } },
      { provide: AccountDeskRecoveryStore, useValue: recovery },
      { provide: Router, useValue: { navigate: vi.fn() } },
    ],
  });
}

function recoveryStore(overrides: Record<string, unknown> = {}) {
  return {
    requestAutomationChange: vi.fn(),
    requestJournalCure: vi.fn(),
    requestLegacyRetirement: vi.fn(),
    requestEmergencyFlatten: vi.fn(),
    refreshLegacyCandidates: vi.fn(),
    cancelConfirmation: vi.fn(),
    confirm: vi.fn(),
    setExposureOverrideReason: vi.fn(),
    setConfirmationToken: vi.fn(),
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
