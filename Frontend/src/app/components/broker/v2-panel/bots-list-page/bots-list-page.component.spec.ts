import { fireEvent, render, screen, within } from '@testing-library/angular';
import { HttpErrorResponse } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import { describe, expect, it, vi } from 'vitest';
import { MessageService } from 'primeng/api';

import type { BrokerAccountSnapshot, ClerkStatus } from '../../../../api/alpaca.types';
import { BrokersService } from '../../../../services/brokers.service';
import { healthyAccountOperatorPostureFixture } from '../../../../testing/operator-blocker-fixtures';
import {
  fakeBotPanelView,
  fakeCatalogBot,
  fakePanelAction,
} from '../../../../testing/bot-panel-fixtures';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import type { BotCatalogView, PanelAction } from '../lib/broker-v2-panel.types';
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
    operator_posture: healthyAccountOperatorPostureFixture(),
  };
}


async function renderPage(
  bots: BotCatalogView[] = [],
  overrides: {
    account?: BrokerAccountSnapshot;
    clerk?: ClerkStatus;
    getCatalog?: (broker: string, accountId: string) => Promise<BotCatalogView[]>;
    panelActions?: PanelAction[];
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
    getPanel: vi.fn((_b: string, _a: string, sid: string) =>
      Promise.resolve(
        fakeBotPanelView({ strategy_instance_id: sid, actions: overrides.panelActions ?? [] }),
      ),
    ),
    getEvidence: vi.fn((_b: string, _a: string, _sid: string) =>
      Promise.resolve({ entries: [], next_cursor: null }),
    ),
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

  it('carries the backend status label on the rail row', async () => {
    await renderPage([fakeCatalogBot({ status_label: 'Working', phase: 'ON_DUTY', running: true })]);

    expect(await screen.findByRole('heading', { name: 'Running · 1' })).toBeTruthy();
    expect(screen.getByText(/^Working ·/)).toBeTruthy();
  });

  it('names attention state on the rail row rather than relying on colour', async () => {
    await renderPage([fakeCatalogBot({ needs_attention: true })]);

    expect(
      await screen.findByRole('button', { name: /spy-momentum-01, needs attention/ }),
    ).toBeTruthy();
  });

  it('presents the selected bot\'s panel actions in the detail pane', async () => {
    await renderPage([fakeCatalogBot()], { panelActions: [fakePanelAction('stop')] });

    expect(await screen.findByRole('button', { name: 'Stop' })).toBeTruthy();
  });

  it('keeps the fee disclaimer beside the P&L it qualifies', async () => {
    await renderPage();

    expect(await screen.findByText(/Fees not reported/i)).toBeTruthy();
  });

  it('auto-selects the most urgent bot so the detail pane is never blank', async () => {
    await renderPage([
      fakeCatalogBot({ strategy_instance_id: 'calm-bot' }),
      fakeCatalogBot({ strategy_instance_id: 'urgent-bot', needs_attention: true }),
    ]);

    const selected = await screen.findByRole('button', { name: /urgent-bot/ });
    expect(selected.getAttribute('aria-current')).toBe('true');
  });

  it('renders empty state message when no bots', async () => {
    await renderPage([]);

    expect(await screen.findByText(/No Alpaca bots yet/i)).toBeTruthy();
  });

  it('renders explicit refresh and snapshot freshness', async () => {
    await renderPage([fakeCatalogBot()]);

    expect(await screen.findByRole('button', { name: 'Refresh fleet' })).toBeTruthy();
    expect((await screen.findAllByText(/Updated/i)).length).toBeGreaterThan(0);
  });

  it('opens and closes Deploy strategy over the Bots list', async () => {
    await renderPage([]);

    fireEvent.click(await screen.findByRole('button', { name: /Deploy strategy/i }));

    expect(await screen.findByRole('heading', { name: 'Deploy a bot' })).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Close deploy a bot' }));

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
        fakeCatalogBot({
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
      fakeCatalogBot({ strategy_instance_id: 'pa10-bot', account_id: 'PA10' }),
    ]);
    expect(await screen.findByText('pa10-bot')).toBeTruthy();
  });

  /**
   * The triage board reads one panel per *selection*. An action must still
   * submit straight from the action the pane already holds — no extra
   * preflight read between the click and the POST.
   */
  it('submits the presented action without an extra panel preflight', async () => {
    const view = await renderPage([fakeCatalogBot()], { panelActions: [fakePanelAction('stop')] });

    const stop = await screen.findByRole('button', { name: 'Stop' });
    const readsBeforeClick = view.mockPanelService.getPanel.mock.calls.length;
    fireEvent.click(stop);

    await vi.waitFor(() => expect(view.mockPanelService.runBotAction).toHaveBeenCalledOnce());
    expect(view.mockPanelService.runBotAction).toHaveBeenCalledWith(
      'alpaca',
      'PA9',
      'spy-momentum-01',
      expect.objectContaining({ action_id: 'stop', concurrency_token: 'stop-token' }),
      null,
    );
    expect(view.mockPanelService.getPanel.mock.calls.length).toBe(readsBeforeClick);
    expect(view.mockMessageService.add).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'success', detail: 'ok' }),
    );
  });

  /**
   * The detail pane's journal read is audit-logged: `read_evidence_page`
   * appends an `EvidenceAuditEntry` naming the operator and the instant. The
   * refresh token is page-global, so bumping it after the operator moved on
   * would assert they read the *newly selected* bot's evidence, which they
   * never did.
   */
  it('does not refresh another bot\'s audited evidence when an action settles', async () => {
    let settleAction = (): void => undefined;
    const view = await renderPage(
      [
        fakeCatalogBot({ strategy_instance_id: 'bot-a' }),
        fakeCatalogBot({ strategy_instance_id: 'bot-b' }),
      ],
      { panelActions: [fakePanelAction('stop')] },
    );
    view.mockPanelService.runBotAction.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          settleAction = () => resolve({ message: 'ok' } as never);
        }),
    );

    fireEvent.click(await screen.findByRole('button', { name: 'Stop' }));
    await vi.waitFor(() => expect(view.mockPanelService.runBotAction).toHaveBeenCalledOnce());

    // The operator moves on to another bot while that action is still running,
    // and opens its Operator lens — the only lens that reads the audit-logged
    // custody journal — so bot-b has a genuine baseline read.
    fireEvent.click(await screen.findByRole('button', { name: /bot-b/ }));
    fireEvent.click(await screen.findByRole('tab', { name: 'Operator' }));
    await vi.waitFor(() =>
      expect(view.mockPanelService.getEvidence).toHaveBeenCalledWith(
        'alpaca',
        'PA9',
        'bot-b',
        expect.anything(),
      ),
    );
    const readsOfB = view.mockPanelService.getEvidence.mock.calls.filter(
      (call) => call[2] === 'bot-b',
    ).length;

    settleAction();
    await vi.waitFor(() => expect(view.mockMessageService.add).toHaveBeenCalled());

    const readsOfBAfter = view.mockPanelService.getEvidence.mock.calls.filter(
      (call) => call[2] === 'bot-b',
    ).length;
    expect(readsOfBAfter).toBe(readsOfB);
  });

  it('toasts the backend-authored reason and refreshes the fleet on a rejected action', async () => {
    const view = await renderPage([fakeCatalogBot()], {
      panelActions: [fakePanelAction('resume')],
    });
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

    fireEvent.click(await screen.findByRole('button', { name: 'Resume' }));

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
