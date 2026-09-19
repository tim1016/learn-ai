import { fireEvent, render, screen } from '@testing-library/angular';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { SqliteExtendedLimitPricing } from '../../../../api/alpaca.types';
import { ExtendedFlattenTicketComponent } from './extended-flatten-ticket.component';

const NOW_MS = 1_753_794_000_000;

function pricing(overrides: Partial<SqliteExtendedLimitPricing> = {}): SqliteExtendedLimitPricing {
  return {
    kind: 'extended_limit',
    phase: 'PRE',
    symbol: 'SPY',
    side: 'sell',
    bid: 512.31,
    ask: 512.36,
    bid_size: 300,
    ask_size: null,
    quote_observed_at_ms: NOW_MS,
    quote_max_age_ms: 10_000,
    exit_allowance_bps: 20,
    suggested_limit_price: 511.28,
    ...overrides,
  };
}

async function renderTicket(value: SqliteExtendedLimitPricing = pricing()) {
  const send = vi.fn();
  const view = await render(ExtendedFlattenTicketComponent, {
    inputs: { pricing: value, quantity: 10 },
    on: { send },
  });
  return { ...view, send };
}

function limitInput(): HTMLInputElement {
  const input = screen.getByLabelText('Limit price (USD)');
  if (!(input instanceof HTMLInputElement)) throw new Error('limit price is not an input');
  return input;
}

describe('ExtendedFlattenTicketComponent', () => {
  afterEach(() => vi.useRealTimers());

  it('shows the live bid, ask and spread beside the suggested limit', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(NOW_MS + 2_000);
    await renderTicket();

    expect(screen.getByText('Pre-market')).toBeTruthy();
    expect(screen.getByText(/\$512\.31/)).toBeTruthy();
    expect(screen.getByText('× 300')).toBeTruthy();
    expect(screen.getByText(/\$512\.36/)).toBeTruthy();
    expect(screen.getByText(/\$0\.05/)).toBeTruthy();
    expect(screen.getByText(/2 s old/)).toBeTruthy();
    expect(screen.getByText(/bid − 20 bps/)).toBeTruthy();
    expect(limitInput().value).toBe('511.28');
  });

  it('sends the suggested limit only after an explicit confirm naming the order', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(NOW_MS);
    const { send, fixture } = await renderTicket();

    fireEvent.click(screen.getByRole('button', { name: 'Review limit order' }));
    fixture.detectChanges();
    expect(send).not.toHaveBeenCalled();
    expect(screen.getByText(/Sell 10 SPY at limit \$511\.28, extended hours, good for today\./))
      .toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Send limit order' }));

    expect(send).toHaveBeenCalledExactlyOnceWith(511.28);
  });

  it('keeps an edited price across quote refreshes and warns when it may not fill', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(NOW_MS);
    const { send, fixture } = await renderTicket();

    fireEvent.input(limitInput(), { target: { value: '512.50' } });
    fixture.componentRef.setInput('pricing', pricing({ bid: 512.4, suggested_limit_price: 511.37 }));
    fixture.detectChanges();

    expect(limitInput().value).toBe('512.50');
    expect(screen.getByText(/Above the bid: this sells only if a buyer pays your price/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Review limit order' }));
    fixture.detectChanges();
    fireEvent.click(screen.getByRole('button', { name: 'Send limit order' }));
    expect(send).toHaveBeenCalledExactlyOnceWith(512.5);
  });

  it('refuses to send against a quote older than the Clerk accepts', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(NOW_MS + 10_001);
    await renderTicket();

    expect(screen.getByText(/more than 10 seconds old/)).toBeTruthy();
    const review = screen.getByRole('button', { name: 'Review limit order' });
    expect(review.hasAttribute('disabled')).toBe(true);
  });

  it('refuses a price that is not a positive number', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(NOW_MS);
    const { fixture } = await renderTicket();

    fireEvent.input(limitInput(), { target: { value: 'abc' } });
    fixture.detectChanges();

    expect(screen.getByRole('alert').textContent).toContain('Enter a positive limit price.');
    expect(screen.getByRole('button', { name: 'Review limit order' }).hasAttribute('disabled'))
      .toBe(true);
  });

  it('prices a buy to cover from the ask', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(NOW_MS);
    const { fixture } = await renderTicket(
      pricing({ side: 'buy', phase: 'POST', suggested_limit_price: 513.39 }),
    );

    expect(screen.getByText('After-hours')).toBeTruthy();
    expect(screen.getByText(/ask \+ 20 bps/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Review limit order' }));
    fixture.detectChanges();
    expect(screen.getByText(/Buy to cover 10 SPY at limit \$513\.39/)).toBeTruthy();
  });
});
