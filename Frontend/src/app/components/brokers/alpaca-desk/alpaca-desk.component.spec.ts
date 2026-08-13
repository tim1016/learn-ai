import { fireEvent, render, screen } from '@testing-library/angular';
import { HttpErrorResponse } from '@angular/common/http';
import { ActivatedRoute, convertToParamMap, provideRouter, Router } from '@angular/router';
import { of } from 'rxjs';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { BrokersService } from '../../../services/brokers.service';
import { BrokerV2PanelService } from '../../broker/v2-panel/lib/broker-v2-panel.service';
import { AlpacaDeskComponent } from './alpaca-desk.component';

const LENS_STORAGE_KEY = 'learn-ai.alpaca-desk.lens';

function brokerService() {
  return {
    getAccount: vi.fn().mockResolvedValue({
      broker: 'alpaca',
      account_id: 'PA1',
      account_mode: 'paper',
      account_status: 'ACTIVE',
      currency: 'USD',
      cash: 1_000,
      equity: 1_000,
      buying_power: 2_000,
      portfolio_value: 1_000,
      long_market_value: 0,
      short_market_value: 0,
      pattern_day_trader: false,
      trading_blocked: false,
      account_blocked: false,
      created_at_ms: null,
      observed_at_ms: 1,
    }),
    listPositions: vi.fn().mockResolvedValue([]),
    listActivities: vi.fn().mockResolvedValue([]),
    getClerkStatus: vi.fn().mockResolvedValue({
      broker: 'alpaca',
      account_id: 'PA1',
      hold: { active: false, reason_code: null, reason: null, since_ms: null },
      latest_reconciliation: null,
      outstanding_intents: 0,
      observed_at_ms: 1,
    }),
    getCustodyDiagnosis: vi.fn().mockResolvedValue({
      broker: 'alpaca',
      account_id: 'PA1',
      in_sync: true,
      observed_at_ms: 1,
      snapshot_version: 'v1',
      resolution_posture: 'paper',
      resolvable: false,
      blocked_reason: null,
      divergences: [],
      resolution_plan: [],
    }),
    getSqliteClerkProjection: vi.fn().mockResolvedValue({}),
    getSqliteManualOrderCapability: vi.fn().mockResolvedValue({
      available: false,
      unavailable: {
        code: 'MANUAL_TRADING_NOT_QUALIFIED',
        message: 'Manual SQLite trading remains disabled until paper qualification is complete.',
      },
      supported_order_shape: 'BUY market DAY equity, one leg',
    }),
    getSqliteManualOrderTicket: vi.fn().mockRejectedValue(new HttpErrorResponse({ status: 404 })),
    previewSqliteManualOrder: vi.fn(),
    submitSqliteManualOrder: vi.fn(),
  };
}

function manualTicketQuery(overrides: Record<string, string> = {}): Record<string, string> {
  return {
    order: 'new',
    accountId: 'PA1',
    symbol: 'SPY',
    ticketId: '7de3a77c-b698-4e0d-a5d1-2f624574ed35',
    legId: '09d6d63e-6375-4e6d-8d20-3b1bf70c2465',
    ...overrides,
  };
}

async function renderDesk(
  query: Record<string, string> = {},
  brokers = brokerService(),
) {
  const queryParamMap = convertToParamMap(query);
  const view = await render(AlpacaDeskComponent, {
    providers: [
      provideRouter([]),
      {
        provide: ActivatedRoute,
        useValue: {
          queryParamMap: of(queryParamMap),
          snapshot: { queryParamMap },
        },
      },
      { provide: BrokersService, useValue: brokers },
      {
        provide: BrokerV2PanelService,
        useValue: { getDeployView: () => new Promise<never>(() => undefined) },
      },
    ],
  });

  return { brokers, router: view.fixture.debugElement.injector.get(Router), view };
}

describe('AlpacaDeskComponent', () => {
  beforeEach(() => localStorage.clear());

  it('defaults to the Trader lens while restoring account safety surfaces', async () => {
    const { brokers } = await renderDesk();

    expect(screen.getByRole('tab', { name: 'Trader' }).getAttribute('aria-selected')).toBe('true');
    expect(screen.getByRole('heading', { name: 'Trader desk' })).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'Operator desk' })).toBeNull();
    expect(await screen.findByLabelText('Clerk and broker in sync')).toBeTruthy();
    expect(brokers.getClerkStatus).toHaveBeenCalledOnce();
    expect(brokers.getSqliteClerkProjection).not.toHaveBeenCalled();
  });

  it('switches instantly, updates the query parameter, persists, and lazy-loads operator data', async () => {
    const { brokers, router } = await renderDesk();
    await screen.findByText('PA1');

    fireEvent.click(screen.getByRole('tab', { name: 'Operator' }));

    expect(screen.getByRole('heading', { name: 'Operator desk' })).toBeTruthy();
    expect(localStorage.getItem(LENS_STORAGE_KEY)).toBe('operator');
    await vi.waitFor(() => expect(router.url).toContain('lens=operator'));
    await vi.waitFor(() => expect(brokers.getClerkStatus).toHaveBeenCalledTimes(2));
    expect(brokers.getAccount).toHaveBeenCalledOnce();

    fireEvent.click(screen.getByRole('tab', { name: 'Trader' }));
    fireEvent.click(screen.getByRole('tab', { name: 'Operator' }));

    expect(brokers.getClerkStatus).toHaveBeenCalledTimes(2);
    expect(brokers.getAccount).toHaveBeenCalledOnce();
  });

  it('opens the Operator lens from a query deep link', async () => {
    const { brokers } = await renderDesk({ lens: 'operator' });

    expect(screen.getByRole('tab', { name: 'Operator' }).getAttribute('aria-selected')).toBe('true');
    expect(screen.getByRole('heading', { name: 'Operator desk' })).toBeTruthy();
    await vi.waitFor(() => expect(brokers.getClerkStatus).toHaveBeenCalledTimes(2));
  });

  it('opens Deploy strategy from the desk and closes back to the visible desk', async () => {
    const { router } = await renderDesk();

    fireEvent.click(screen.getByRole('button', { name: 'Deploy strategy' }));

    expect(await screen.findByRole('heading', { name: 'Deploy a bot' })).toBeTruthy();
    await vi.waitFor(() => expect(router.url).toContain('deploy='));

    fireEvent.click(screen.getByRole('button', { name: 'Close deploy strategy' }));

    expect(screen.queryByRole('heading', { name: 'Deploy a bot' })).toBeNull();
    expect(screen.getByRole('heading', { name: 'Alpaca' })).toBeTruthy();
    await vi.waitFor(() => expect(router.url).not.toContain('deploy'));
  });

  it('opens the Deploy drawer from a query deep link', async () => {
    await renderDesk({ deploy: '' });

    expect(await screen.findByRole('heading', { name: 'Deploy a bot' })).toBeTruthy();
  });

  it('opens a matching manual-order deep link under legacy authority', async () => {
    const brokers = brokerService();
    brokers.getSqliteManualOrderCapability.mockRejectedValue(
      new HttpErrorResponse({ status: 409 }),
    );

    await renderDesk(manualTicketQuery({ symbol: 'spy' }), brokers);

    expect(await screen.findByText('Create Alpaca order')).toBeTruthy();
    expect(await screen.findByDisplayValue('SPY')).toBeTruthy();
  });

  it('refuses a manual-order link for a different account', async () => {
    const { brokers } = await renderDesk(manualTicketQuery({ accountId: 'PA-OTHER' }));

    expect((await screen.findByRole('alert')).textContent).toContain(
      'The order link targets account PA-OTHER, but Alpaca is connected to PA1. No ticket was opened.',
    );
    expect(screen.queryByRole('heading', { name: 'Create Alpaca order' })).toBeNull();
    expect(brokers.getSqliteManualOrderCapability).not.toHaveBeenCalled();
  });

  it('opens a SQLite ticket with the server-owned disabled reason', async () => {
    const { brokers } = await renderDesk(manualTicketQuery());

    expect(
      await screen.findByText(/Manual SQLite trading remains disabled until paper qualification is complete/),
    ).toBeTruthy();
    expect(await screen.findByText('Create Alpaca order')).toBeTruthy();
    expect(brokers.getSqliteManualOrderCapability).toHaveBeenCalledWith('PA1');
  });

  it('restores the last selected lens when no query parameter is present', async () => {
    localStorage.setItem(LENS_STORAGE_KEY, 'operator');

    await renderDesk();

    expect(screen.getByRole('heading', { name: 'Operator desk' })).toBeTruthy();
  });

  it('moves focus and selection with the lens tab keyboard controls', async () => {
    await renderDesk();
    const traderTab = screen.getByRole('tab', { name: 'Trader' });
    const operatorTab = screen.getByRole('tab', { name: 'Operator' });

    traderTab.focus();
    fireEvent.keyDown(traderTab, { key: 'ArrowRight' });

    expect(document.activeElement).toBe(operatorTab);
    expect(operatorTab.getAttribute('aria-selected')).toBe('true');
  });
});
