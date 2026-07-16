import { signal } from '@angular/core';
import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { AccountDeskRecoveryStore } from './account-desk-recovery-store.service';
import { AccountDeskLegacyClaimCureComponent } from './account-desk-legacy-claim-cure.component';

describe('AccountDeskLegacyClaimCureComponent', () => {
  it('renders only backend candidates without changing opaque audit tokens', async () => {
    const candidate = {
      claim_id: 'legacy:opaque/1', strategy_instance_id: 'retired-bot', run_id: 'run:opaque/1',
      bot_order_namespace: 'learn-ai/retired-bot/v1', symbol: 'SPY', claimed_quantity: 2,
      proof_summary: 'LEGACY_CLAIM_BROKER_FLAT:SPY', proved_at_ms: 1_780_000_000_000,
      confirmation: {
        title: 'Retire legacy stale claim', body: 'Backend body.', consequence: 'Backend consequence.',
        confirm_label: 'Retire stale claim', required_token: '',
      },
    };
    const recovery = {
      busy: signal(false), legacyCandidates: signal([candidate]), legacyLoading: signal(false),
      legacyErrorMessage: signal<string | null>(null), requestLegacyRetirement: vi.fn(), refreshLegacyCandidates: vi.fn(),
    };
    await render(AccountDeskLegacyClaimCureComponent, {
      providers: [{ provide: AccountDeskRecoveryStore, useValue: recovery }],
    });

    expect(await screen.findByText('legacy:opaque/1')).toBeTruthy();
    expect(screen.getByText('run:opaque/1')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Review retirement' }));

    expect(recovery.requestLegacyRetirement).toHaveBeenCalledWith(candidate);
  });
});
