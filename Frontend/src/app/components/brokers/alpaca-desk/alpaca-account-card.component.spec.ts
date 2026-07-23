import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { BrokerAccountSnapshot } from '../../../api/alpaca.types';
import { BrokersService } from '../../../services/brokers.service';
import { AlpacaAccountCardComponent } from './alpaca-account-card.component';

function fakeAccount(overrides: Partial<BrokerAccountSnapshot> = {}): BrokerAccountSnapshot {
  return {
    broker: 'alpaca',
    account_id: 'PA9',
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

    expect(await screen.findByText('Account PA9')).toBeTruthy();
    expect(screen.getByText('Paper')).toBeTruthy();
    expect(screen.getByText('Buying power')).toBeTruthy();
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
});
