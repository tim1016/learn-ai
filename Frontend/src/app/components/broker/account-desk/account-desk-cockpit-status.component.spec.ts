import { signal } from '@angular/core';
import { Router } from '@angular/router';
import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { AccountCockpitResponse } from '../../../api/account-cockpit.types';
import { AccountDeskDirectoryStore } from './account-desk-directory-store.service';
import { AccountDeskCockpitStatusComponent } from './account-desk-cockpit-status.component';
import { AccountDeskRecoveryStore } from './account-desk-recovery-store.service';

describe('AccountDeskCockpitStatusComponent', () => {
  it('renders the backend-declared Restore Clerk card and forwards its exact blocker for confirmation', async () => {
    const recovery = { requestCockpitMove: vi.fn() };
    await render(AccountDeskCockpitStatusComponent, {
      providers: [
        { provide: AccountDeskDirectoryStore, useValue: directory(cockpit('CLERK_DOWN')) },
        { provide: AccountDeskRecoveryStore, useValue: recovery },
        { provide: Router, useValue: { navigate: vi.fn() } },
      ],
    });

    expect(screen.getByText(/Account Clerk Unavailable/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Restore Clerk' }));
    expect(recovery.requestCockpitMove).toHaveBeenCalledWith(expect.objectContaining({
      blocker: expect.objectContaining({ condition: expect.objectContaining({ id: 'ACCOUNT_CLERK_UNAVAILABLE' }) }),
    }));
  });

  it('renders host-side guidance when the daemon is down and deliberately offers no restart control', async () => {
    await render(AccountDeskCockpitStatusComponent, {
      providers: [
        { provide: AccountDeskDirectoryStore, useValue: directory(cockpit('DAEMON_DOWN')) },
        { provide: AccountDeskRecoveryStore, useValue: { requestCockpitMove: vi.fn() } },
        { provide: Router, useValue: { navigate: vi.fn() } },
      ],
    });

    expect(screen.getByText(/Daemon Unreachable/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Open host recovery guidance' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: /restart/i })).toBeNull();
  });
});

function directory(value: AccountCockpitResponse) {
  return {
    cockpit: signal(value),
    serviceStatusLoading: signal(false),
    serviceStatusHasLastGood: signal(true),
    serviceStatusErrorMessage: signal<string | null>(null),
    serviceStatusShowingStaleLastGood: signal(false),
    retryServiceStatus: vi.fn(),
  };
}

function cockpit(mode: AccountCockpitResponse['mode']): AccountCockpitResponse {
  const daemonDown = mode === 'DAEMON_DOWN';
  const clerkDown = mode === 'CLERK_DOWN';
  const reasonCode = daemonDown ? 'DAEMON_UNREACHABLE' : 'ACCOUNT_CLERK_UNAVAILABLE';
  return {
    schema_version: 1,
    account_id: 'DU1234567',
    generated_at_ms: 1_780_000_000_000,
    mode,
    clerk: {
      schema_version: 3,
      account_id: 'DU1234567',
      attachment: clerkDown ? 'UNATTACHED' : 'ATTACHED',
      phase: clerkDown ? null : 'accepting',
      generation: clerkDown ? null : 4,
      generation_recorded_at_ms: null,
      source: null,
      binding: {
        state: clerkDown ? 'UNATTACHED' : 'ATTACHED', generation: null, lease_generation: null,
        pending_retirement_proposals: 0, ledger_read_authority: 'clerk_ledger', ledger_parity: 'clean', ledger_parity_issue_count: 0,
      },
      gate_authority: {
        requested_authority: 'account_truth', effective_authority: 'account_truth', promotion_state: 'SAFE_DEFAULT',
        reason_code: 'ACCOUNT_GATE_SAFE_DEFAULT', disposition: null, action_authority: 'account_truth',
        action_gate: {
          gate_id: 'account.account_truth', status: 'pass', source: 'test', operator_reason: 'PASS',
          operator_next_step: 'No action.', evidence_at_ms: 1_780_000_000_000,
        }, observed_session_dates: [], lease_weaker_comparison_count: 0, restart_smoke_recorded_at_ms: null,
      },
      session_policy: {
        allow_outside_live_session: false,
        gate_result: {
          gate_id: 'account.live_session', status: 'pass', source: 'test', operator_reason: 'PASS',
          operator_next_step: 'No action.', evidence_at_ms: 1_780_000_000_000,
        },
      },
      lease: null,
      journal: { last_seq: null, last_write_ms: null },
      operating_state: clerkDown ? 'ATTENTION' : 'STANDBY',
      headline: clerkDown ? 'Account Clerk needs attention' : 'Ready — no bots on duty',
      detail: 'Backend-authored posture.',
    },
    daemon: {
      availability: daemonDown ? 'DOWN' : 'AVAILABLE', reason_code: reasonCode,
      detail: daemonDown ? 'The host daemon did not answer.' : 'The host daemon is reachable.',
      observed_at_ms: 1_780_000_000_000,
    },
    blockers: [{
      condition: { id: reasonCode, severity: 'blocking', scope: daemonDown ? 'host' : 'account', evidence: {} },
      host: 'account_desk', anchor: { kind: 'surface', subject_key: null }, audience: 'both',
      disposition: daemonDown ? 'fix_elsewhere' : 'fix_here', headline: daemonDown ? 'Host daemon needs host-side recovery' : 'Account Clerk is unavailable',
      detail: 'Backend-authored guidance.', applies_to: 'both', secondary_moves: [],
      primary_move: daemonDown
        ? { label: 'Open host recovery guidance', action: { kind: 'navigate', route: '/broker/session-mirror', fragment: null }, target: null, confirmation: null }
        : {
          label: 'Restore Clerk', action: { kind: 'confirm_in_form', anchor: 'account-clerk-restore-action' }, target: null,
          confirmation: { title: 'Restore Account Clerk', body: 'Backend preview.', consequence: 'Backend consequence.', confirm_label: 'Restore Clerk', required_token: 'RESTORE' },
        },
    }],
  };
}
