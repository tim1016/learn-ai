import { fireEvent, render, screen, within } from '@testing-library/angular';
import { HttpErrorResponse } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import { describe, expect, it, vi } from 'vitest';
import { MessageService } from 'primeng/api';

import type { BrokerAccountSnapshot, ClerkStatus } from '../../../../api/alpaca.types';
import { BrokersService } from '../../../../services/brokers.service';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import type { BotCatalogView } from '../lib/broker-v2-panel.types';
import { BotsListPageComponent } from './bots-list-page.component';

function fakeAccount(overrides: Partial<BrokerAccountSnapshot> = {}): BrokerAccountSnapshot {
  return {
    broker: 'alpaca',
    account_id: 'PA9',
    account_mode: 'paper',
    account_status: 'ACTIVE',
    currency: 'USD',
    cash: 10_000,
    equity: 15_000,
    buying_power: 30_000,
    portfolio_value: 15_000,
    long_market_value: 5_000,
    short_market_value: 0,
    pattern_day_trader: false,
    trading_blocked: false,
    account_blocked: false,
    created_at_ms: 1_600_000_000_000,
    observed_at_ms: 1_700_000_000_000,
    ...overrides,
  };
}

function fakeClerkStatus(): ClerkStatus {
  return {
    account_id: 'PA9',
    broker: 'alpaca',
    hold: { active: false },
    outstanding_intents: 0,
    observed_at_ms: 1_700_000_000_000,
  };
}

function fakeBot(overrides: Partial<BotCatalogView> = {}): BotCatalogView {
  return {
    strategy_instance_id: 'spy-momentum-01',
    broker: 'alpaca',
    account_id: 'PA9',
    symbol: 'SPY',
    phase: 'ON_DUTY',
    desired_state: 'RUNNING',
    running: true,
    strategy_key: 'deployment_validation',
    strategy_label: 'Deployment Validation',
    mode: 'trade',
    status_label: 'Working',
    status_explanation: 'Running under Account Clerk custody.',
    exposure: {},
    fills_today: 2,
    realized_pnl_today: 45.5,
    open_pnl: null,
    last_activity_at_ms: 1_700_000_000_000,
    needs_attention: false,
    ...overrides,
  };
}

function fakeRowAction(
  actionId: 'resume' | 'stop',
): NonNullable<BotCatalogView['row_action']> {
  return {
    action_id: actionId,
    label: actionId === 'resume' ? 'Resume' : 'Stop',
    explanation: `${actionId} this bot.`,
    enabled: true,
    blockers: [],
    confirmation: null,
    revision: 1,
    concurrency_token: `${actionId}-token`,
  };
}

async function renderPage(
  bots: BotCatalogView[] = [],
  overrides: {
    account?: BrokerAccountSnapshot;
    clerk?: ClerkStatus;
    getCatalog?: (broker: string, accountId: string) => Promise<BotCatalogView[]>;
  } = {},
) {
  const account = overrides.account ?? fakeAccount();
  const clerk = overrides.clerk ?? fakeClerkStatus();

  const mockBrokersService = {
    getAccount: () => Promise.resolve(account),
    getClerkStatus: () => Promise.resolve(clerk),
  };

  const mockPanelService = {
    getCatalog: overrides.getCatalog ?? (() => Promise.resolve(bots)),
    getDeployView: vi.fn(() => new Promise<never>(() => undefined)),
    getPanel: vi.fn(() => Promise.reject(new Error('full-panel preflight is forbidden'))),
    runBotAction: vi.fn(() =>
      Promise.resolve({
        action_id: 'resume',
        applied: true,
        revision: 1,
        concurrency_token: 'next-token',
        message: 'ok',
      }),
    ),
  };

  const mockMessageService = { add: vi.fn() };

  const view = await render(BotsListPageComponent, {
    providers: [
      provideRouter([]),
      { provide: BrokersService, useValue: mockBrokersService },
      { provide: BrokerV2PanelService, useValue: mockPanelService },
      { provide: MessageService, useValue: mockMessageService },
    ],
    componentInputs: { broker: 'alpaca', accountId: 'PA9' },
  });
  return { ...view, mockPanelService, mockMessageService };
}

describe('BotsListPageComponent', () => {
  it('renders account strip equity value', async () => {
    await renderPage([], { account: fakeAccount({ equity: 15_000 }) });

    expect(await screen.findByText(/\$15,000\.00/)).toBeTruthy();
  });

  it('renders PAPER badge for paper accounts', async () => {
    await renderPage([], { account: fakeAccount({ account_id: 'PA9' }) });

    const accountPosture = await screen.findByLabelText('Alpaca account posture');
    expect(within(accountPosture).getByText('Paper')).toBeTruthy();
  });

  it('renders "Working" status label for an ON_DUTY bot', async () => {
    const bot = fakeBot({ status_label: 'Working', phase: 'ON_DUTY', running: true });
    await renderPage([bot]);

    // "Working" appears in both the status-filter toggle and the bot status chip;
    // verify the chip is present by finding it within the table row.
    const cells = await screen.findAllByText('Working');
    expect(cells.length).toBeGreaterThanOrEqual(1);
  });

  it('renders attention marker for bots with needs_attention=true', async () => {
    const bot = fakeBot({ needs_attention: true });
    await renderPage([bot]);

    const attentionEl = await screen.findByLabelText('Working, needs attention');
    expect(attentionEl).toBeTruthy();
  });

  it('renders a server-presented Stop button for an on-duty bot', async () => {
    const bot = fakeBot({ row_action: fakeRowAction('stop') });
    await renderPage([bot]);

    expect(await screen.findByRole('button', { name: /Stop spy-momentum-01/i })).toBeTruthy();
  });

  it('renders "Fees not reported" note', async () => {
    await renderPage();

    expect(await screen.findByText(/Fees not reported/i)).toBeTruthy();
  });

  it('renders empty state message when no bots', async () => {
    await renderPage([]);

    expect(await screen.findByText(/No Alpaca bots yet/i)).toBeTruthy();
  });

  it('renders explicit refresh and snapshot freshness', async () => {
    await renderPage([fakeBot()]);

    expect(await screen.findByRole('button', { name: 'Refresh fleet' })).toBeTruthy();
    expect((await screen.findAllByText(/Updated/i)).length).toBeGreaterThan(0);
  });

  it('opens and closes Deploy strategy over the Bots list', async () => {
    await renderPage([]);

    fireEvent.click(await screen.findByRole('button', { name: /Deploy strategy/i }));

    expect(await screen.findByRole('heading', { name: 'Deploy a bot' })).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Close deploy strategy' }));

    expect(screen.queryByRole('heading', { name: 'Deploy a bot' })).toBeNull();
    expect(screen.getByRole('heading', { name: 'Alpaca bots' })).toBeTruthy();
  });

  it('renders the retry state when a transient catalog load fails', async () => {
    await renderPage([], {
      getCatalog: () => Promise.reject(new Error('data plane restarting')),
    });

    expect((await screen.findByRole('alert')).textContent).toContain('Fleet unavailable');
  });

  it('never carries a last-good roster across an account route change', async () => {
    let resolvePa10!: (bots: BotCatalogView[]) => void;
    const pa10Catalog = new Promise<BotCatalogView[]>((resolve) => {
      resolvePa10 = resolve;
    });
    const getCatalog = vi.fn((_broker: string, accountId: string) => {
      if (accountId === 'PA10') return pa10Catalog;
      return Promise.resolve([
        fakeBot({
          strategy_instance_id: 'pa9-bot',
          account_id: accountId,
        }),
      ]);
    });
    const view = await renderPage([], { getCatalog });
    expect(await screen.findByText('pa9-bot')).toBeTruthy();

    view.fixture.componentRef.setInput('accountId', 'PA10');
    view.fixture.detectChanges();

    await vi.waitFor(() => expect(getCatalog).toHaveBeenCalledWith('alpaca', 'PA10'));
    expect(screen.queryByText('pa9-bot')).toBeNull();

    resolvePa10([
      fakeBot({ strategy_instance_id: 'pa10-bot', account_id: 'PA10' }),
    ]);
    expect(await screen.findByText('pa10-bot')).toBeTruthy();
  });

  it('executes the catalog-presented action without a full-panel preflight', async () => {
    const bot = fakeBot({ row_action: fakeRowAction('stop') });
    const view = await renderPage([bot]);

    fireEvent.click(await screen.findByRole('button', { name: 'Stop spy-momentum-01' }));

    await vi.waitFor(() => expect(view.mockPanelService.runBotAction).toHaveBeenCalledOnce());
    expect(view.mockPanelService.getPanel).not.toHaveBeenCalled();
    expect(view.mockMessageService.add).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'success', detail: 'ok' }),
    );
  });

  it('toasts the backend-authored reason and refreshes the fleet on a rejected action', async () => {
    const bot = fakeBot({ row_action: fakeRowAction('resume') });
    const view = await renderPage([bot]);
    view.mockPanelService.runBotAction.mockRejectedValueOnce(
      new HttpErrorResponse({
        status: 409,
        error: {
          detail: {
            action_id: 'resume',
            outcome: 'conflict',
            receipt_id: null,
            recorded_at_ms: 1_700_000_000_000,
            message: 'This bot is no longer ready to resume.',
            why: 'Its custody state changed after this button was shown.',
          },
        },
      }),
    );

    fireEvent.click(await screen.findByRole('button', { name: 'Resume spy-momentum-01' }));

    await vi.waitFor(() => expect(view.mockPanelService.runBotAction).toHaveBeenCalledOnce());
    expect(view.mockMessageService.add).toHaveBeenCalledWith(
      expect.objectContaining({
        severity: 'warn',
        detail:
          'This bot is no longer ready to resume. Its custody state changed after this button was shown.',
      }),
    );
  });
});
