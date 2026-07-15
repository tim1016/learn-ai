import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { BrokerService } from '../../../services/broker.service';
import { JournalClaimCureComponent } from './journal-claim-cure.component';

describe('JournalClaimCureComponent', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('previews a server-owned claim and appends a bounded immutable cure', async () => {
    const broker = {
      previewJournalCure: vi.fn().mockResolvedValue({
        account_id: 'DU1234567',
        bot_order_namespace: 'learn-ai/bot-a/v1',
        symbol: 'SPY',
        journal_quantity: 2,
        required_adjustment_sign: 'negative',
        can_cure: true,
        reason_code: 'JOURNAL_CURE_CLAIM_REDUCIBLE',
      }),
      applyJournalCure: vi.fn().mockResolvedValue({
        schema_version: 1,
        account_id: 'DU1234567',
        bot_order_namespace: 'learn-ai/bot-a/v1',
        symbol: 'SPY',
        signed_quantity: -2,
        operator_attribution: 'local-operator',
        request_provenance: 'account-monitor/journal-cure',
        reason: 'Broker fill was already reconciled.',
        evidence_refs: ['account-reconciliation:receipt-1'],
        idempotency_key: 'cure-1',
        recorded_at_ms: 1_780_000_000_000,
        journal_seq: 4,
      }),
    };
    vi.stubGlobal('crypto', { randomUUID: () => 'cure-1' });

    await render(JournalClaimCureComponent, {
      componentInputs: { accountId: 'DU1234567' },
      providers: [{ provide: BrokerService, useValue: broker }],
    });

    fireEvent.input(screen.getByLabelText('Namespace'), {
      target: { value: 'learn-ai/bot-a/v1' },
    });
    fireEvent.input(screen.getByLabelText('Symbol'), { target: { value: 'spy' } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview claim' }));

    expect(await screen.findByText(/Journal claim 2 for SPY/)).toBeTruthy();
    fireEvent.input(screen.getByLabelText('Signed correction'), { target: { value: '-2' } });
    fireEvent.input(screen.getByLabelText('Evidence reference'), {
      target: { value: 'account-reconciliation:receipt-1' },
    });
    fireEvent.input(screen.getByLabelText('Reason'), {
      target: { value: 'Broker fill was already reconciled.' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Append cure' }));

    await waitFor(() => {
      expect(broker.applyJournalCure).toHaveBeenCalledWith('DU1234567', {
        bot_order_namespace: 'learn-ai/bot-a/v1',
        symbol: 'SPY',
        signed_quantity: -2,
        reason: 'Broker fill was already reconciled.',
        evidence_refs: ['account-reconciliation:receipt-1'],
        request_provenance: 'account-monitor/journal-cure',
        idempotency_key: 'cure-1',
      });
    });
    expect(await screen.findByText('Cure journaled at sequence 4.')).toBeTruthy();
  });

  it('reuses the prepared idempotency key when a cure response is lost', async () => {
    const broker = {
      previewJournalCure: vi.fn().mockResolvedValue({
        account_id: 'DU1234567',
        bot_order_namespace: 'learn-ai/bot-a/v1',
        symbol: 'SPY',
        journal_quantity: 2,
        required_adjustment_sign: 'negative',
        can_cure: true,
        reason_code: 'JOURNAL_CURE_CLAIM_REDUCIBLE',
      }),
      applyJournalCure: vi.fn()
        .mockRejectedValueOnce(new Error('response lost'))
        .mockResolvedValueOnce({
          schema_version: 1,
          account_id: 'DU1234567',
          bot_order_namespace: 'learn-ai/bot-a/v1',
          symbol: 'SPY',
          signed_quantity: -2,
          operator_attribution: 'local-operator',
          request_provenance: 'account-monitor/journal-cure',
          reason: 'verified',
          evidence_refs: ['proof'],
          idempotency_key: 'first-attempt', recorded_at_ms: 1, journal_seq: 4,
        }),
    };
    vi.stubGlobal('crypto', {
      randomUUID: vi.fn().mockReturnValueOnce('first-attempt').mockReturnValueOnce('second-attempt'),
    });
    await render(JournalClaimCureComponent, {
      componentInputs: { accountId: 'DU1234567' },
      providers: [{ provide: BrokerService, useValue: broker }],
    });
    fireEvent.input(screen.getByLabelText('Namespace'), {
      target: { value: 'learn-ai/bot-a/v1' },
    });
    fireEvent.input(screen.getByLabelText('Symbol'), { target: { value: 'SPY' } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview claim' }));
    await screen.findByText(/Journal claim 2 for SPY/);
    fireEvent.input(screen.getByLabelText('Signed correction'), { target: { value: '-2' } });
    fireEvent.input(screen.getByLabelText('Evidence reference'), { target: { value: 'proof' } });
    fireEvent.input(screen.getByLabelText('Reason'), { target: { value: 'verified' } });
    fireEvent.click(screen.getByRole('button', { name: 'Append cure' }));
    await waitFor(() => expect(broker.applyJournalCure).toHaveBeenCalledTimes(1));
    fireEvent.input(screen.getByLabelText('Reason'), { target: { value: 'verified after response loss' } });
    fireEvent.click(screen.getByRole('button', { name: 'Append cure' }));
    await waitFor(() => expect(broker.applyJournalCure).toHaveBeenCalledTimes(2));

    expect(broker.applyJournalCure.mock.calls.map((call) => call[1].idempotency_key)).toEqual(['first-attempt', 'first-attempt']);
  });

  it('does not issue a second cure request while the first is pending', async () => {
    let release: (() => void) | undefined;
    const pending = new Promise<void>((resolve) => {
      release = resolve;
    });
    const broker = {
      previewJournalCure: vi.fn().mockResolvedValue({
        account_id: 'DU1234567',
        bot_order_namespace: 'learn-ai/bot-a/v1',
        symbol: 'SPY',
        journal_quantity: 2,
        required_adjustment_sign: 'negative',
        can_cure: true,
        reason_code: 'JOURNAL_CURE_CLAIM_REDUCIBLE',
      }),
      applyJournalCure: vi.fn().mockImplementation(async () => {
        await pending;
        return {
          schema_version: 1,
          account_id: 'DU1234567',
          bot_order_namespace: 'learn-ai/bot-a/v1',
          symbol: 'SPY',
          signed_quantity: -2,
          operator_attribution: 'local-operator',
          request_provenance: 'account-monitor/journal-cure',
          reason: 'verified',
          evidence_refs: ['proof'],
          idempotency_key: 'attempt',
          recorded_at_ms: 1,
          journal_seq: 4,
        };
      }),
    };
    vi.stubGlobal('crypto', { randomUUID: () => 'attempt' });
    await render(JournalClaimCureComponent, {
      componentInputs: { accountId: 'DU1234567' },
      providers: [{ provide: BrokerService, useValue: broker }],
    });
    fireEvent.input(screen.getByLabelText('Namespace'), { target: { value: 'learn-ai/bot-a/v1' } });
    fireEvent.input(screen.getByLabelText('Symbol'), { target: { value: 'SPY' } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview claim' }));
    await screen.findByText(/Journal claim 2 for SPY/);
    fireEvent.input(screen.getByLabelText('Signed correction'), { target: { value: '-2' } });
    fireEvent.input(screen.getByLabelText('Evidence reference'), { target: { value: 'proof' } });
    fireEvent.input(screen.getByLabelText('Reason'), { target: { value: 'verified' } });

    fireEvent.click(screen.getByRole('button', { name: 'Append cure' }));
    fireEvent.click(screen.getByRole('button', { name: 'Append cure' }));

    await waitFor(() => expect(broker.applyJournalCure).toHaveBeenCalledTimes(1));
    release?.();
    await screen.findByText('Cure journaled at sequence 4.');
  });
});
