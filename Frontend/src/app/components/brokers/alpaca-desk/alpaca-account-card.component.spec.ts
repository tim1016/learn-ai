import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { BrokerAccountSnapshot } from '../../../api/alpaca.types';
import { BrokersService } from '../../../services/brokers.service';
import { AlpacaAccountCardComponent } from './alpaca-account-card.component';

function fakeAccount(overrides: Partial<BrokerAccountSnapshot> = {}): BrokerAccountSnapshot {
  return {
    broker: 'alpaca',
    account_id: 'PA9',
    account_mode: 'paper',
    account_status: 'ACTIVE',
    currency: 'USD',
    cash: 100,
    equity: 150,
    buying_power: 300,
    portfolio_value: 150,
    long_market_value: 50,
    short_market_value: 0,
    pattern_day_trader: false,
    trading_blocked: false,
    account_blocked: false,
    created_at_ms: 1_600_000_000_000,
    observed_at_ms: 1_700_000_000_000,
    ...overrides,
  };
}

async function renderCard(getAccount: () => Promise<BrokerAccountSnapshot>) {
  return render(AlpacaAccountCardComponent, {
    providers: [{ provide: BrokersService, useValue: { getAccount } }],
  });
}

describe('AlpacaAccountCardComponent', () => {
  it('renders account figures and a paper badge when loaded', async () => {
    await renderCard(() => Promise.resolve(fakeAccount({ account_id: 'PA9', buying_power: 300 })));

    expect(await screen.findByText('PA9')).toBeTruthy();
    expect(screen.getByText('Paper')).toBeTruthy();
    expect(screen.getByText('Equity')).toBeTruthy();
    expect(screen.getByText('Cash')).toBeTruthy();
    expect(screen.getByText('Buying power')).toBeTruthy();
    expect(screen.getByText('Updated (local)')).toBeTruthy();

    screen.getByRole('button', { name: 'Account details' }).click();
    expect(screen.getByText('Portfolio value')).toBeTruthy();
  });

  it('renders an error state, distinct from empty, when Alpaca is unreachable', async () => {
    await renderCard(() => Promise.reject(new Error('unreachable')));

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain("Couldn't reach Alpaca");
  });

  it('renders the account status through the receiptLabel pipe', async () => {
    await renderCard(() => Promise.resolve(fakeAccount({ account_status: 'ACTIVE' })));

    // receiptLabel title-cases the code identifier.
    expect(await screen.findByText('Active')).toBeTruthy();
  });

  it('tags a live account as Live with danger severity, never a hardcoded Paper', async () => {
    await renderCard(() => Promise.resolve(fakeAccount({ account_id: '9LIVE0001', account_mode: 'live' })));

    expect(await screen.findByText('9LIVE0001')).toBeTruthy();
    expect(screen.getByText('Live')).toBeTruthy();
    expect(screen.queryByText('Paper')).toBeNull();
  });

  it('renders the margin facts read-only, with a dash for an unknown value', async () => {
    await renderCard(() =>
      Promise.resolve(
        fakeAccount({
          multiplier: 4,
          regt_buying_power: 200_000,
          daytrading_buying_power: null,
          maintenance_margin: 0,
          initial_margin: 0,
          sma: 100_000,
          last_equity: 100_000,
        }),
      ),
    );

    expect(await screen.findByText('Multiplier')).toBeTruthy();
    expect(screen.getByText('Multiplier').nextElementSibling?.textContent).toContain('4');
    expect(screen.getByText('Day-trading BP').nextElementSibling?.textContent).toContain('—');
  });

  it('seats the margin panel after the disclosure toggle in DOM order', async () => {
    const { container } = await renderCard(() => Promise.resolve(fakeAccount({ multiplier: 4 })));

    const multiplier = await screen.findByText('Multiplier');
    const toggle = container.querySelector('.account-toggle');
    if (!(toggle instanceof HTMLElement)) throw new Error('account toggle not rendered');
    expect(Boolean(toggle.compareDocumentPosition(multiplier) & Node.DOCUMENT_POSITION_FOLLOWING)).toBe(true);
  });
});
