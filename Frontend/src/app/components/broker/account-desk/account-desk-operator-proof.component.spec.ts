import { signal } from '@angular/core';
import { Router } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { AccountReconciliationReceipt } from '../../../api/account-reconciliation.types';
import { makeCleanAccountTriage } from '../testing/account-triage-fixtures';
import { AccountDeskGuidanceStore } from './account-desk-guidance-store.service';
import { AccountDeskOperatorProofComponent } from './account-desk-operator-proof.component';
import { AccountDeskSurfaceStore } from './account-desk-surface-store.service';

describe('AccountDeskOperatorProofComponent', () => {
  it('condenses reconciliation facts and keeps raw audit references disclosed', async () => {
    const receipt: AccountReconciliationReceipt = {
      schema_version: 1,
      receipt_id: 'acct-recon-DU1234567-1',
      account_id: 'DU1234567',
      requested_account_id: 'DU1234567',
      connected_account_id: 'DU1234567',
      state: 'NOT_PROVEN',
      account_truth_verdict: 'not_proven',
      account_truth_severity: 'warning',
      final_gate_result: {
        gate_id: 'account.reconciliation',
        status: 'block',
        source: 'account_truth',
        operator_reason: 'Position data is stale.',
        operator_next_step: 'Refresh account proof.',
        evidence_at_ms: 1_780_000_000_000,
      },
      exposure_resolution: 'flat',
      account_truth: {} as AccountReconciliationReceipt['account_truth'],
      evidence_refs: [
        { source: 'account_truth', ref: 'account_truth:1780000000000', detail: null },
        { source: 'account_truth.blocker', ref: 'source_freshness_positions_stale', detail: null },
      ],
      generated_at_ms: 1_780_000_000_000,
      account_truth_generated_at_ms: 1_780_000_000_000,
      expires_at_ms: 1_780_000_060_000,
      ttl_ms: 60_000,
    };
    const triage = makeCleanAccountTriage({ receipt });
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

    expect((await screen.findAllByText('Not Proven')).length).toBe(2);
    expect(screen.getByText('Block')).toBeTruthy();
    expect(screen.getByText('Position data is stale.')).toBeTruthy();
    expect(screen.getByText('Show technical audit references')).toBeTruthy();
    expect(document.querySelector('[data-timestamp-mode="local"]')).not.toBeNull();
  });

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
