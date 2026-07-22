import { provideZonelessChangeDetection, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  AccountRecoveryFlattenCandidate,
  AccountReconciliationReceipt,
  AccountTriageResponse,
  JournalCurePreview,
  LegacyStaleClaimCandidate,
} from '../../../api/account-reconciliation.types';
import type { AccountCockpitResponse } from '../../../api/account-cockpit.types';
import type { OperatorBlockerMoveEvent } from '../shared/operator-blocker-list/operator-blocker-list.component';
import { BrokerService } from '../../../services/broker.service';
import { makeCleanAccountTriage } from '../testing/account-triage-fixtures';
import { AccountDeskEventsStore } from './account-desk-events-store.service';
import { AccountDeskDirectoryStore } from './account-desk-directory-store.service';
import { AccountDeskRecoveryStore } from './account-desk-recovery-store.service';
import { AccountDeskSurfaceStore } from './account-desk-surface-store.service';

describe('AccountDeskRecoveryStore', () => {
  const broker = {
    reconcileAccount: vi.fn(),
    updateAccountReconciliationAutomation: vi.fn(),
    clearAccountFreeze: vi.fn(),
    acceptExposureOverride: vi.fn(),
    previewJournalCure: vi.fn(),
    applyJournalCure: vi.fn(),
    legacyStaleClaimCandidates: vi.fn(),
    retireLegacyStaleClaim: vi.fn(),
    submitOperatorRecoveryFlatten: vi.fn(),
    emergencyFlattenAccount: vi.fn(),
    restoreAccountClerk: vi.fn(),
    recoverAccountJournal: vi.fn(),
  };
  const surface = { load: vi.fn(), triage: signal<AccountTriageResponse | null>(null) };
  const events = { load: vi.fn() };
  const directory = { cockpit: signal<AccountCockpitResponse | null>(null), loadServiceStatus: vi.fn() };

  beforeEach(() => {
    Object.values(broker).forEach((method) => method.mockReset());
    broker.legacyStaleClaimCandidates.mockResolvedValue({ account_id: 'DU1234567', candidates: [] });
    surface.load.mockReset().mockResolvedValue(undefined);
    surface.triage = signal(null);
    events.load.mockReset().mockResolvedValue(undefined);
    directory.cockpit = signal(null);
    directory.loadServiceStatus.mockReset().mockResolvedValue(undefined);
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        AccountDeskRecoveryStore,
        { provide: BrokerService, useValue: broker },
        { provide: AccountDeskSurfaceStore, useValue: surface },
        { provide: AccountDeskEventsStore, useValue: events },
        { provide: AccountDeskDirectoryStore, useValue: directory },
      ],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  it('opens only a server-declared account-desk confirmation and cancellation sends no mutation', () => {
    const store = TestBed.inject(AccountDeskRecoveryStore);
    store.load('DU1234567');

    store.requestDeclaredMove(move('account-reconciliation-action'));
    expect(store.confirmation()?.command).toBe('reconcile');
    store.cancelConfirmation();
    expect(store.confirmation()).toBeNull();
    expect(broker.reconcileAccount).not.toHaveBeenCalled();

    store.requestDeclaredMove(move('unknown-account-action'));
    expect(store.confirmation()).toBeNull();
  });

  it('preserves a successful reconciliation receipt and refreshes proof and timeline', async () => {
    const store = TestBed.inject(AccountDeskRecoveryStore);
    broker.reconcileAccount.mockResolvedValue(receipt());
    store.load('DU1234567');
    store.requestDeclaredMove(move('account-reconciliation-action'));

    await store.confirm();

    expect(broker.reconcileAccount).toHaveBeenCalledWith('DU1234567');
    expect(store.success()?.kind).toBe('reconcile');
    expect(surface.load).toHaveBeenCalledWith('DU1234567');
    expect(events.load).toHaveBeenCalledWith('DU1234567');
  });

  it('confirms the exact backend policy change before sending it', async () => {
    const store = TestBed.inject(AccountDeskRecoveryStore);
    broker.updateAccountReconciliationAutomation.mockResolvedValue({
      account_id: 'DU1234567', enabled: true, updated_at_ms: 1_780_000_000_000, updated_by: 'account-desk.operator',
    });
    store.load('DU1234567');
    store.requestAutomationChange({
      schema_version: 1, account_id: 'DU1234567', enabled: false, updated_at_ms: 0, updated_by: 'system.default',
    });

    expect(store.confirmation()?.desiredAutomationEnabled).toBe(true);
    await store.confirm();

    expect(broker.updateAccountReconciliationAutomation).toHaveBeenCalledWith('DU1234567', {
      enabled: true, updated_by: 'account-desk.operator',
    });
  });

  it('requires an operator-entered reason for a declared exposure override', async () => {
    const store = TestBed.inject(AccountDeskRecoveryStore);
    broker.acceptExposureOverride.mockResolvedValue({
      schema_version: 1, account_id: 'DU1234567', cleared: true, cleared_source: 'account_audited_override', override_id: 'override-1', triage: {},
    });
    store.load('DU1234567');
    store.requestDeclaredMove(move('account-exposure-override-action'));

    expect(store.canConfirm()).toBe(false);
    store.setExposureOverrideReason('Operator reviewed the projected exposure.');
    await store.confirm();

    expect(broker.acceptExposureOverride).toHaveBeenCalledWith('DU1234567', {
      requested_by: 'account-desk.operator', reason: 'Operator reviewed the projected exposure.',
    });
  });

  it('replaces control-plane secret failures with operator-safe recovery guidance', async () => {
    const store = TestBed.inject(AccountDeskRecoveryStore);
    broker.acceptExposureOverride.mockRejectedValue({
      error: { detail: 'missing or wrong X-Data-Plane-Control-Secret' },
    });
    store.load('DU1234567');
    store.requestDeclaredMove(move('account-exposure-override-action'));
    store.setExposureOverrideReason('Operator reviewed the projected exposure.');

    await store.confirm();

    expect(store.errorMessage()).toBe(
      'The secure control connection is unavailable. Ask a platform operator to restore it, then try again.',
    );
  });

  it('keeps a rejected recovery visibly distinct from success without refreshing the verdict', async () => {
    const store = TestBed.inject(AccountDeskRecoveryStore);
    broker.clearAccountFreeze.mockRejectedValue({ error: { detail: 'The reconciliation receipt has expired.' } });
    store.load('DU1234567');
    store.requestDeclaredMove(move('account-clear-freeze-action'));

    await store.confirm();

    expect(store.success()).toBeNull();
    expect(store.errorMessage()).toBe('The reconciliation receipt has expired.');
    expect(surface.load).not.toHaveBeenCalled();
    expect(events.load).not.toHaveBeenCalled();
  });

  it('does not attach a late mutation result to a newly selected account', async () => {
    const deferred = promise<AccountReconciliationReceipt>();
    const store = TestBed.inject(AccountDeskRecoveryStore);
    broker.reconcileAccount.mockReturnValue(deferred.promise);
    store.load('DU1234567');
    store.requestDeclaredMove(move('account-reconciliation-action'));
    const confirmation = store.confirm();
    store.load('DU7654321');
    deferred.resolve(receipt());
    await confirmation;

    expect(store.success()).toBeNull();
    expect(surface.load).not.toHaveBeenCalledWith('DU7654321');
  });

  it('requires the exact backend token before confirming a dangerous action', async () => {
    const store = TestBed.inject(AccountDeskRecoveryStore);
    broker.reconcileAccount.mockResolvedValue(receipt());
    store.load('DU1234567');
    store.requestDeclaredMove(move('account-reconciliation-action', null, 'HALT'));

    expect(store.canConfirm()).toBe(false);
    store.setConfirmationToken('WRONG');
    expect(store.canConfirm()).toBe(false);
    store.setConfirmationToken('HALT');
    await store.confirm();

    expect(broker.reconcileAccount).toHaveBeenCalledWith('DU1234567');
  });

  it('restores only the backend-declared Clerk card with its typed token, durable receipt, and re-observation', async () => {
    const cockpit = restoreCockpit();
    directory.cockpit.set(cockpit);
    broker.restoreAccountClerk.mockResolvedValue({
      schema_version: 1, receipt_id: 'account-clerk-restore:opaque/1', account_id: 'DU1234567',
      clerk_generation: 3, recorded_at_ms: 1_780_000_000_000,
    });
    const store = TestBed.inject(AccountDeskRecoveryStore);
    store.load('DU1234567');
    const [blocker] = cockpit.blockers;
    if (blocker.primary_move === null) throw new Error('Expected the backend restore move.');

    store.requestCockpitMove({ blocker, move: blocker.primary_move });
    expect(store.confirmation()?.command).toBe('restore_clerk');
    await store.confirm();
    expect(broker.restoreAccountClerk).not.toHaveBeenCalled();

    store.setConfirmationToken('RESTORE');
    await store.confirm();

    expect(broker.restoreAccountClerk).toHaveBeenCalledWith('DU1234567', {
      confirmation_token: 'RESTORE',
      idempotency_key: expect.any(String),
    });
    expect(store.success()?.kind).toBe('restore_clerk');
    expect(directory.loadServiceStatus).toHaveBeenCalledWith('DU1234567');
  });

  it('executes the ordered journal ceremony only from its backend-declared card and token', async () => {
    const cockpit = journalRecoveryCockpit();
    directory.cockpit.set(cockpit);
    broker.recoverAccountJournal.mockResolvedValue({
      receipt_id: 'journal-recovery-quarantine:opaque/1', account_id: 'DU1234567', phase: 'REBASELINE_REQUIRED',
      recorded_at_ms: 1_780_000_000_000, quarantined_journal_name: 'clerk_journal.jsonl.corrupt-opaque',
      broker_evidence_positions: [],
    });
    const store = TestBed.inject(AccountDeskRecoveryStore);
    store.load('DU1234567');
    const [blocker] = cockpit.blockers;
    if (blocker.primary_move === null) throw new Error('Expected the backend journal recovery move.');

    store.requestCockpitMove({ blocker, move: blocker.primary_move });
    expect(store.confirmation()?.command).toBe('journal_recovery');
    await store.confirm();
    expect(broker.recoverAccountJournal).not.toHaveBeenCalled();

    store.setConfirmationToken('QUARANTINE');
    await store.confirm();

    expect(broker.recoverAccountJournal).toHaveBeenCalledWith('DU1234567', 'quarantine', {
      confirmation_token: 'QUARANTINE', idempotency_key: expect.any(String),
    });
    expect(store.success()?.kind).toBe('journal_recovery');
  });

  it('confirms the exact fresh journal preview, preserves its receipt, and refreshes shared proof', async () => {
    const store = TestBed.inject(AccountDeskRecoveryStore);
    const preview = journalPreview();
    broker.applyJournalCure.mockResolvedValue({
      schema_version: 1,
      account_id: 'DU1234567',
      bot_order_namespace: preview.bot_order_namespace,
      symbol: preview.symbol,
      signed_quantity: -2,
      operator_attribution: 'local-operator',
      request_provenance: 'account-desk/journal-cure',
      reason: 'Operator reviewed the fresh claim.',
      evidence_refs: ['receipt:opaque/1'],
      idempotency_key: 'journal-key',
      recorded_at_ms: 1_780_000_000_000,
      journal_seq: 9,
    });
    store.load('DU1234567');

    store.requestJournalCure(preview, -2, 'Operator reviewed the fresh claim.', 'receipt:opaque/1');
    await store.confirm();

    expect(broker.applyJournalCure).toHaveBeenCalledWith('DU1234567', expect.objectContaining({
      bot_order_namespace: preview.bot_order_namespace,
      symbol: preview.symbol,
      signed_quantity: -2,
      evidence_refs: ['receipt:opaque/1'],
    }));
    expect(store.success()?.kind).toBe('journal_cure');
    expect(surface.load).toHaveBeenCalledWith('DU1234567');
    expect(events.load).toHaveBeenCalledWith('DU1234567');
  });

  it('keeps a preview-to-confirm drift rejection distinct from a journal-cure success', async () => {
    const store = TestBed.inject(AccountDeskRecoveryStore);
    broker.applyJournalCure.mockRejectedValue({ error: { detail: { message: 'The Clerk preview is stale.' } } });
    store.load('DU1234567');
    store.requestJournalCure(journalPreview(), -2, 'Operator reviewed the fresh claim.', 'receipt:opaque/1');

    await store.confirm();

    expect(store.success()).toBeNull();
    expect(store.errorMessage()).toBe('The Clerk preview is stale.');
    expect(surface.load).not.toHaveBeenCalled();
    expect(events.load).not.toHaveBeenCalled();
  });

  it('requires a currently returned legacy candidate and allows cancellation without a mutation', async () => {
    const candidate = legacyCandidate();
    broker.legacyStaleClaimCandidates.mockResolvedValue({
      account_id: 'DU1234567', generated_at_ms: 1_780_000_000_000, candidates: [candidate],
    });
    const store = TestBed.inject(AccountDeskRecoveryStore);
    store.load('DU1234567');
    await Promise.resolve();

    store.requestLegacyRetirement(candidate);
    expect(store.confirmation()?.legacyCandidate?.claim_id).toBe(candidate.claim_id);
    store.cancelConfirmation();

    expect(broker.retireLegacyStaleClaim).not.toHaveBeenCalled();
  });

  it('submits recovery flatten only when its backend action target matches a current exact candidate', async () => {
    const candidate = recoveryFlattenCandidate();
    surface.triage.set(makeCleanAccountTriage({
      accountId: 'DU1234567',
      recoveryFlattenCandidates: [candidate],
    }));
    broker.submitOperatorRecoveryFlatten.mockResolvedValue({
      recovery_flatten: {
        status: 'recovery_flattened',
        recorded: { intent_id: candidate.intent.intent_id, order_ref: candidate.intent.order_ref, journal_seq: 4, recorded_at_ms: 1_780_000_000_000 },
        broker_acked: { intent_id: candidate.intent.intent_id, order_ref: candidate.intent.order_ref, journal_seq: 5, recorded_at_ms: 1_780_000_000_001, order_id: 12, perm_id: null, exec_id: null },
        cancelled_order_ids: [],
      },
    });
    const store = TestBed.inject(AccountDeskRecoveryStore);
    store.load('DU1234567');
    store.requestDeclaredMove(move('account-recovery-flatten-action', candidate.intent.intent_id));

    expect(store.confirmation()?.command).toBe('recovery_flatten');
    await store.confirm();

    expect(broker.submitOperatorRecoveryFlatten).toHaveBeenCalledWith('DU1234567', {
      intent: candidate.intent,
      request_provenance: 'account-desk/recovery-flatten',
    });
  });

  it('submits an account emergency flatten only after typed confirmation', async () => {
    const store = TestBed.inject(AccountDeskRecoveryStore);
    broker.emergencyFlattenAccount.mockResolvedValue({
      accepted: true,
      account_id: 'DU1234567',
      audit_run_id: 'eflat-audit-1',
      completed_at_ms: 1_780_000_000_000,
    });
    store.load('DU1234567');
    store.requestEmergencyFlatten({
      title: 'Emergency flatten paper account',
      body: 'Backend body.',
      consequence: 'Backend consequence.',
      confirm_label: 'Emergency flatten account',
      required_token: 'FLATTEN',
    });

    expect(store.canConfirm()).toBe(false);
    store.setConfirmationToken('FLATTEN');
    await store.confirm();

    expect(broker.emergencyFlattenAccount).toHaveBeenCalledWith('DU1234567', {
      account: 'DU1234567',
      confirmation_token: 'FLATTEN',
      idempotency_key: expect.any(String),
    });
    expect(store.success()?.kind).toBe('emergency_flatten');
  });
});

function move(anchor: string, target: string | null = null, requiredToken = ''): OperatorBlockerMoveEvent {
  return {
    blocker: {
      condition: { id: 'condition-1', severity: 'blocking', scope: 'account', evidence: {} },
      host: 'account_desk', anchor: { kind: 'cure_tools', subject_key: null }, audience: 'operator', disposition: 'fix_here',
      headline: 'Backend authored headline', detail: 'Backend authored detail', primary_move: null, secondary_moves: [], applies_to: 'both',
    },
    move: {
      label: 'Backend authored move', action: { kind: 'confirm_in_form', anchor }, target,
      confirmation: {
        title: 'Backend authored title', body: 'Backend authored body', consequence: 'Backend authored consequence', confirm_label: 'Confirm', required_token: requiredToken,
      },
    },
  };
}

function restoreCockpit(): AccountCockpitResponse {
  const confirmation = {
    title: 'Restore Account Clerk', body: 'Backend preview.', consequence: 'Backend consequence.',
    confirm_label: 'Restore Clerk', required_token: 'RESTORE',
  };
  return {
    schema_version: 1,
    account_id: 'DU1234567',
    generated_at_ms: 1_780_000_000_000,
    mode: 'CLERK_DOWN',
    clerk: {
      schema_version: 3, account_id: 'DU1234567', attachment: 'UNATTACHED', phase: null, generation: null,
      generation_recorded_at_ms: null, source: null,
      binding: {
        state: 'UNATTACHED', generation: null, lease_generation: null, pending_retirement_proposals: 0,
        ledger_read_authority: 'legacy_registry', ledger_parity: 'clean', ledger_parity_issue_count: 0,
      },
      gate_authority: {
        requested_authority: 'account_truth', effective_authority: 'account_truth', promotion_state: 'SAFE_DEFAULT',
        reason_code: 'ACCOUNT_GATE_SAFE_DEFAULT', disposition: null, action_authority: 'account_truth',
        action_gate: {
          gate_id: 'account.account_truth', status: 'block', source: 'test', operator_reason: 'ACCOUNT_TRUTH_NOT_AVAILABLE',
          operator_next_step: 'Refresh Account Truth.', evidence_at_ms: 1_780_000_000_000,
        }, observed_session_dates: [], lease_weaker_comparison_count: 0, restart_smoke_recorded_at_ms: null,
      },
      session_policy: {
        allow_outside_live_session: false,
        gate_result: {
          gate_id: 'account.live_session', status: 'block', source: 'test', operator_reason: 'OUTSIDE_LIVE_TRADABLE_SESSION',
          operator_next_step: 'Wait for a live session.', evidence_at_ms: 1_780_000_000_000,
        },
      },
      lease: null, journal: { last_seq: null, last_write_ms: null }, operating_state: 'ATTENTION',
      headline: 'Account Clerk is unavailable', detail: 'Backend-authored posture.',
    },
    daemon: {
      availability: 'AVAILABLE', reason_code: 'DAEMON_CONNECTED', detail: 'The host daemon is reachable.',
      observed_at_ms: 1_780_000_000_000,
    },
    blockers: [{
      condition: { id: 'ACCOUNT_CLERK_UNAVAILABLE', severity: 'blocking', scope: 'account', evidence: {} },
      host: 'account_desk', anchor: { kind: 'surface', subject_key: null }, audience: 'both',
      disposition: 'fix_here', headline: 'Account Clerk is unavailable', detail: 'Backend-authored guidance.',
      primary_move: {
        label: 'Restore Clerk', action: { kind: 'confirm_in_form', anchor: 'account-clerk-restore-action' },
        target: null, confirmation,
      },
      secondary_moves: [], applies_to: 'both',
    }],
  };
}

function journalRecoveryCockpit(): AccountCockpitResponse {
  const clerkDown = restoreCockpit();
  return {
    ...clerkDown,
    mode: 'JOURNAL_CORRUPT',
    clerk: {
      ...clerkDown.clerk,
      attachment: 'ATTACHED',
      journal: {
        last_seq: null, last_write_ms: null, integrity: 'corrupt',
        corruption_detail: 'invalid row at line 1', recovery_phase: 'QUARANTINE_REQUIRED',
      },
    },
    blockers: [{
      condition: { id: 'ACCOUNT_CLERK_JOURNAL_CORRUPT', severity: 'blocking', scope: 'account', evidence: {} },
      host: 'account_desk', anchor: { kind: 'surface', subject_key: null }, audience: 'operator', disposition: 'fix_here',
      headline: 'Account Clerk journal is corrupt — broker writes are blocked', detail: 'Backend-authored guidance.', applies_to: 'both', secondary_moves: [],
      primary_move: {
        label: 'Begin journal recovery ceremony', action: { kind: 'confirm_in_form', anchor: 'account-journal-recovery-action' }, target: null,
        confirmation: {
          title: 'Quarantine corrupt Clerk journal', body: 'Backend preview.', consequence: 'Backend consequence.',
          confirm_label: 'Quarantine journal', required_token: 'QUARANTINE',
        },
      },
    }],
  };
}

function journalPreview(): JournalCurePreview {
  return {
    account_id: 'DU1234567', bot_order_namespace: 'learn-ai/retired-bot/v1', symbol: 'SPY', journal_quantity: 2,
    required_adjustment_sign: 'negative', can_cure: true, reason_code: 'JOURNAL_CURE_CLAIM_REDUCIBLE',
    confirmation: {
      title: 'Append Clerk journal cure', body: 'Backend preview body.', consequence: 'Backend consequence.',
      confirm_label: 'Append journal cure', required_token: '',
    },
  };
}

function legacyCandidate(): LegacyStaleClaimCandidate {
  return {
    claim_id: 'legacy:opaque/1', strategy_instance_id: 'retired-bot', run_id: 'run-retired',
    bot_order_namespace: 'learn-ai/retired-bot/v1', symbol: 'SPY', claimed_quantity: 2,
    proof_summary: 'LEGACY_CLAIM_BROKER_FLAT:SPY', proved_at_ms: 1_780_000_000_000,
    confirmation: {
      title: 'Retire legacy stale claim', body: 'Backend candidate body.', consequence: 'Backend consequence.',
      confirm_label: 'Retire stale claim', required_token: '',
    },
  };
}

function recoveryFlattenCandidate(): AccountRecoveryFlattenCandidate {
  return {
    intent: {
      trace_id: 'trace:opaque/1', account_id: 'DU1234567', strategy_instance_id: 'retired-bot', run_id: 'run-retired',
      bot_order_namespace: 'learn-ai/retired-bot/v1', intent_id: 'flatten:opaque/1', order_ref: 'learn-ai/retired-bot/v1:flatten:opaque/1',
      intent_kind: 'RECOVERY_FLATTEN', owner_generation: 7, created_at_ms: 1_780_000_000_000,
      order_spec: {
        symbol: 'SPY', sec_type: 'STK', action: 'SELL', quantity: 2, order_type: 'MKT', limit_price: null,
        time_in_force: 'DAY', outside_rth: false, expiry_ms: null, strike: null, right: null, multiplier: 100,
        confirm_paper: true, client_order_id: 'recovery:opaque/1', order_ref: 'learn-ai/retired-bot/v1:flatten:opaque/1', manual_order: false,
      },
    },
    confirmation: {
      title: 'Submit Clerk recovery flatten', body: 'Backend candidate body.', consequence: 'Backend consequence.',
      confirm_label: 'Submit recovery flatten', required_token: '',
    },
  };
}

function receipt(): AccountReconciliationReceipt {
  return {
    schema_version: 1, receipt_id: 'receipt-1', account_id: 'DU1234567', requested_account_id: 'DU1234567', connected_account_id: 'DU1234567',
    state: 'CLEAN', account_truth_verdict: 'clean', account_truth_severity: 'ok',
    final_gate_result: { gate_id: 'account.reconcile', status: 'pass', source: 'test', operator_reason: 'Backend receipt', operator_next_step: 'NONE', evidence_at_ms: 1_780_000_000_000 },
    exposure_resolution: 'flat', account_truth: {} as AccountReconciliationReceipt['account_truth'], evidence_refs: [],
    generated_at_ms: 1_780_000_000_000, account_truth_generated_at_ms: 1_780_000_000_000, expires_at_ms: 1_780_000_060_000, ttl_ms: 60_000,
  };
}

function promise<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const pending = new Promise<T>((resolvePromise) => { resolve = resolvePromise; });
  return { promise: pending, resolve };
}
