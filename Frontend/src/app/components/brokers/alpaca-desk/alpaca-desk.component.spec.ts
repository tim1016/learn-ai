import { fireEvent, render, screen } from '@testing-library/angular';
import { ActivatedRoute, convertToParamMap, provideRouter, Router } from '@angular/router';
import { of } from 'rxjs';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { BrokersService } from '../../../services/brokers.service';
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
    ],
  });

  return { brokers, router: view.fixture.debugElement.injector.get(Router), view };
}

describe('AlpacaDeskComponent', () => {
  beforeEach(() => localStorage.clear());

  it('defaults to the Trader lens without loading operator data', async () => {
    const { brokers } = await renderDesk();

    expect(screen.getByRole('tab', { name: 'Trader' }).getAttribute('aria-selected')).toBe('true');
    expect(screen.getByRole('heading', { name: 'Trader desk' })).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'Operator desk' })).toBeNull();
    expect(brokers.getClerkStatus).not.toHaveBeenCalled();
  });

  it('switches instantly, updates the query parameter, persists, and lazy-loads operator data', async () => {
    const { brokers, router } = await renderDesk();
    await screen.findByText('PA1');

    fireEvent.click(screen.getByRole('tab', { name: 'Operator' }));

    expect(screen.getByRole('heading', { name: 'Operator desk' })).toBeTruthy();
    expect(localStorage.getItem(LENS_STORAGE_KEY)).toBe('operator');
    await vi.waitFor(() => expect(router.url).toContain('lens=operator'));
    await vi.waitFor(() => expect(brokers.getClerkStatus).toHaveBeenCalledOnce());
    expect(brokers.getAccount).toHaveBeenCalledOnce();

    fireEvent.click(screen.getByRole('tab', { name: 'Trader' }));
    fireEvent.click(screen.getByRole('tab', { name: 'Operator' }));

    expect(brokers.getClerkStatus).toHaveBeenCalledOnce();
    expect(brokers.getAccount).toHaveBeenCalledOnce();
  });

  it('opens the Operator lens from a query deep link', async () => {
    const { brokers } = await renderDesk({ lens: 'operator' });

    expect(screen.getByRole('tab', { name: 'Operator' }).getAttribute('aria-selected')).toBe('true');
    expect(screen.getByRole('heading', { name: 'Operator desk' })).toBeTruthy();
    await vi.waitFor(() => expect(brokers.getClerkStatus).toHaveBeenCalledOnce());
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
