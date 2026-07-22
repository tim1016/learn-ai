import { signal } from '@angular/core';
import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { AccountDeskRecoveryStore } from './account-desk-recovery-store.service';
import { AccountDeskStaleBindingCureComponent } from './account-desk-stale-binding-cure.component';

describe('AccountDeskStaleBindingCureComponent', () => {
  it('renders only backend-proven bindings and sends the exact candidate to confirmation', async () => {
    const candidate = {
      strategy_instance_id: 'audit-dep-only-0717', run_id: 'run:opaque/1',
      bot_order_namespace: 'learn-ai/audit-dep-only-0717/v1', lifecycle_state: 'DEPLOYED' as const,
      source: 'deploy.strategy', proof_summary: 'STALE_BINDING_BROKER_FLAT_AND_PROCESS_EXITED',
      proved_at_ms: 1_780_000_000_000,
      confirmation: {
        title: 'Retire stale deployment binding', body: 'Backend body.', consequence: 'Backend consequence.',
        confirm_label: 'Retire stale binding', required_token: '',
      },
    };
    const recovery = {
      busy: signal(false), staleBindingCandidates: signal([candidate]), staleBindingLoading: signal(false),
      staleBindingErrorMessage: signal<string | null>(null), requestStaleBindingRetirement: vi.fn(),
      refreshStaleBindingCandidates: vi.fn(),
    };
    await render(AccountDeskStaleBindingCureComponent, {
      providers: [{ provide: AccountDeskRecoveryStore, useValue: recovery }],
    });

    expect(await screen.findByText('run:opaque/1')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Review retirement' }));

    expect(recovery.requestStaleBindingRetirement).toHaveBeenCalledWith(candidate);
  });
});
