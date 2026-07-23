import { signal } from '@angular/core';
import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { BrokerService } from '../../../services/broker.service';
import { AccountDeskRecoveryStore } from './account-desk-recovery-store.service';
import { AccountDeskJournalCureComponent } from './account-desk-journal-cure.component';
import { AccountDeskSurfaceStore } from './account-desk-surface-store.service';

describe('AccountDeskJournalCureComponent', () => {
  it('uses the fresh backend preview and sends its exact candidate to the shared confirmation store', async () => {
    const broker = {
      previewJournalCure: vi.fn().mockResolvedValue({
        account_id: 'DU1234567', bot_order_namespace: 'learn-ai/retired-bot/v1', symbol: 'SPY', journal_quantity: 2,
        required_adjustment_sign: 'negative', can_cure: true, reason_code: 'JOURNAL_CURE_CLAIM_REDUCIBLE',
        confirmation: {
          title: 'Append Clerk journal cure', body: 'Backend body.', consequence: 'Backend consequence.',
          confirm_label: 'Append journal cure', required_token: '',
        },
      }),
      accountServiceStatus: vi.fn().mockResolvedValue({
        account_id: 'DU1234567', generation: 4, checked_at_ms: 1_780_000_000_000,
      }),
    };
    const recovery = { busy: signal(false), requestJournalCure: vi.fn() };
    await render(AccountDeskJournalCureComponent, {
      providers: [
        { provide: BrokerService, useValue: broker },
        { provide: AccountDeskSurfaceStore, useValue: { accountId: signal('DU1234567') } },
        { provide: AccountDeskRecoveryStore, useValue: recovery },
      ],
    });

    fireEvent.input(screen.getByLabelText('Namespace'), { target: { value: 'learn-ai/retired-bot/v1' } });
    fireEvent.input(screen.getByLabelText('Symbol'), { target: { value: 'SPY' } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview claim' }));

    expect(await screen.findByRole('button', { name: 'Recheck Clerk' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Recheck Clerk' }));
    expect(await screen.findByText(/Clerk transport is ready/i)).toBeTruthy();
    fireEvent.input(screen.getByLabelText('Signed adjustment'), { target: { value: '-2' } });
    fireEvent.input(screen.getByLabelText('Evidence reference'), { target: { value: 'receipt:opaque/1' } });
    fireEvent.input(screen.getByLabelText('Operator reason'), { target: { value: 'Operator verified the current claim.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Review exact cure' }));

    expect(recovery.requestJournalCure).toHaveBeenCalledWith(
      expect.objectContaining({ bot_order_namespace: 'learn-ai/retired-bot/v1', symbol: 'SPY' }),
      -2,
      'Operator verified the current claim.',
      'receipt:opaque/1',
    );
  });

  it('keeps the cure blocked and renders the backend reason when the host Clerk check fails', async () => {
    const broker = {
      previewJournalCure: vi.fn().mockResolvedValue({
        account_id: 'DU1234567', bot_order_namespace: 'learn-ai/retired-bot/v1', symbol: 'SPY', journal_quantity: 2,
        required_adjustment_sign: 'negative', can_cure: true, reason_code: 'JOURNAL_CURE_CLAIM_REDUCIBLE',
        confirmation: {
          title: 'Append Clerk journal cure', body: 'Backend body.', consequence: 'Backend consequence.',
          confirm_label: 'Append journal cure', required_token: '',
        },
      }),
      accountServiceStatus: vi.fn().mockRejectedValue({
        error: {
          detail: {
            reason_code: 'ACCOUNT_CLERK_UNAVAILABLE:SOCKET_MISSING',
            message: 'The host Clerk socket is not ready.',
          },
        },
      }),
    };
    const recovery = { busy: signal(false), requestJournalCure: vi.fn() };
    await render(AccountDeskJournalCureComponent, {
      providers: [
        { provide: BrokerService, useValue: broker },
        { provide: AccountDeskSurfaceStore, useValue: { accountId: signal('DU1234567') } },
        { provide: AccountDeskRecoveryStore, useValue: recovery },
      ],
    });

    fireEvent.input(screen.getByLabelText('Namespace'), { target: { value: 'learn-ai/retired-bot/v1' } });
    fireEvent.input(screen.getByLabelText('Symbol'), { target: { value: 'SPY' } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview claim' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Recheck Clerk' }));

    expect((await screen.findByRole('alert')).textContent).toContain('The host Clerk socket is not ready.');
    expect(screen.getByText('ACCOUNT_CLERK_UNAVAILABLE:SOCKET_MISSING')).toBeTruthy();
    expect(recovery.requestJournalCure).not.toHaveBeenCalled();
  });
});
