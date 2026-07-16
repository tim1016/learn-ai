import { ActivatedRoute, Router, convertToParamMap } from '@angular/router';
import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { BehaviorSubject } from 'rxjs';
import { describe, expect, it, vi } from 'vitest';

import type { AccountTriageResponse, AccountTriageVerdictState } from '../../../api/account-reconciliation.types';
import { BrokerService } from '../../../services/broker.service';
import { formatReceiptLabel } from '../../../shared/pipes/receipt-label.pipe';
import { formatTimestampDisplay } from '../../../shared/timestamp';
import { makeCleanAccountTriage } from '../testing/account-triage-fixtures';
import { AccountDeskSurfaceStore } from './account-desk-surface-store.service';
import { AccountDeskPageComponent } from './account-desk-page.component';

class FakeBrokerService {
  accountTriage = vi.fn<(accountId: string) => Promise<AccountTriageResponse>>();
}

function triage(state: AccountTriageVerdictState = 'CLEAN'): AccountTriageResponse {
  const current = makeCleanAccountTriage({
    generatedAtMs: 1_780_000_002_000,
    affectedBots: [{
      strategy_instance_id: 'bot-a', run_id: 'run-a', bot_order_namespace: 'learn-ai/bot-a', lifecycle_state: 'ACTIVE',
    }],
  });
  return {
    ...current,
    verdict: {
      state,
      headline: `${state} verdict`,
      detail: `${state} detail`,
      primary_move: state === 'CLEAN' ? null : {
        label: 'Open Account Monitor', route: '/broker/account-monitor', fragment: 'account-reconciliation-action',
      },
      operator_attention_count: state === 'NEEDS_ATTENTION' ? 2 : 0,
    },
  };
}

async function setup(options: { response?: AccountTriageResponse; route$?: BehaviorSubject<ReturnType<typeof convertToParamMap>> } = {}) {
  const broker = new FakeBrokerService();
  broker.accountTriage.mockResolvedValue(options.response ?? triage());
  const route$ = options.route$ ?? new BehaviorSubject(convertToParamMap({ accountId: 'DU1234567' }));
  const router = { navigate: vi.fn().mockResolvedValue(true) };
  const view = await render(AccountDeskPageComponent, {
    providers: [
      AccountDeskSurfaceStore,
      { provide: BrokerService, useValue: broker },
      { provide: ActivatedRoute, useValue: { paramMap: route$.asObservable() } },
      { provide: Router, useValue: router },
    ],
  });
  await screen.findByText((options.response ?? triage()).verdict.headline);
  return { ...view, broker, route$, router };
}

describe('AccountDeskPageComponent', () => {
  it.each(['FROZEN', 'NOT_PROVEN', 'NEEDS_ATTENTION', 'CLEAN'] as const)(
    'renders the server-owned %s verdict without recomputing posture',
    async (state) => {
      await setup({ response: triage(state) });

      expect(screen.getByText(`${state} verdict`)).toBeTruthy();
      expect(screen.getByText(formatReceiptLabel(state))).toBeTruthy();
    },
  );

  it('defaults to the trader lens, keeps the verdict visible, and exposes pressed toggle state', async () => {
    await setup({ response: triage('NEEDS_ATTENTION') });

    const trader = screen.getByRole('button', { name: 'Trader' });
    const operator = screen.getByRole('button', { name: 'Operator' });
    expect(trader.getAttribute('aria-pressed')).toBe('true');
    expect(operator.getAttribute('aria-pressed')).toBe('false');
    fireEvent.click(operator);
    expect(operator.getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByText('NEEDS_ATTENTION verdict')).toBeTruthy();
    expect(screen.getByText('Operator guidance arrives in the operations-lens slice.')).toBeTruthy();
  });

  it('rekeys the route-scoped surface store when the account route changes', async () => {
    const route$ = new BehaviorSubject(convertToParamMap({ accountId: 'DU1234567' }));
    const { broker } = await setup({ route$ });
    broker.accountTriage.mockResolvedValueOnce(makeCleanAccountTriage({ accountId: 'DU7654321' }));

    route$.next(convertToParamMap({ accountId: 'DU7654321' }));
    await waitFor(() => expect(broker.accountTriage).toHaveBeenCalledWith('DU7654321'));
    expect(await screen.findByText('DU7654321')).toBeTruthy();
  });

  it('uses the shared viewer-local timestamp display and preserves stale last-good data with retry', async () => {
    const { broker, fixture } = await setup({ response: triage() });
    expect(screen.getByText(formatTimestampDisplay(1_780_000_002_000, { mode: 'local' }))).toBeTruthy();

    broker.accountTriage.mockRejectedValueOnce(new Error('offline'));
    fixture.componentInstance.retry();
    await screen.findByText(/Showing last good account data/);
    expect(screen.getByText('CLEAN verdict')).toBeTruthy();
  });

  it('shows an explicit empty state and retries an initial fetch failure', async () => {
    const broker = new FakeBrokerService();
    broker.accountTriage.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(makeCleanAccountTriage());
    const route$ = new BehaviorSubject(convertToParamMap({ accountId: 'DU1234567' }));
    await render(AccountDeskPageComponent, {
      providers: [
        AccountDeskSurfaceStore,
        { provide: BrokerService, useValue: broker },
        { provide: ActivatedRoute, useValue: { paramMap: route$.asObservable() } },
        { provide: Router, useValue: { navigate: vi.fn().mockResolvedValue(true) } },
      ],
    });

    const retries = await screen.findAllByRole('button', { name: 'Retry' });
    fireEvent.click(retries[0]);
    await waitFor(() => expect(screen.getByText('No account rows are available yet.')).toBeTruthy());
  });
});
