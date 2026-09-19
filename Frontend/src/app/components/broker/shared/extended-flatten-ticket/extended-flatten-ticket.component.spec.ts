import { ComponentFixture } from '@angular/core/testing';
import { fireEvent, render, screen } from '@testing-library/angular';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type {
  SqliteExtendedLimitPricing,
  SqliteProposedLimitEvaluation,
} from '../../../../api/alpaca.types';
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
    // Twice the allowance below the bid: the furthest the Clerk accepts.
    band_limit_price: 510.26,
    spread: 0.05,
    spread_bps: 0.98,
    wide_spread: false,
    spread_warning_bps: 50,
    proposal: null,
    ...overrides,
  };
}

/** What the Clerk answers when asked to read a price the operator proposed. */
function reading(
  limitPrice: number,
  overrides: Partial<SqliteProposedLimitEvaluation> = {},
): SqliteProposedLimitEvaluation {
  return {
    limit_price: limitPrice,
    through_book_bps: 20.1,
    worst_case_cost: 10.3,
    outside_band: false,
    thin_book: false,
    resting: false,
    ...overrides,
  };
}

async function renderTicket(value: SqliteExtendedLimitPricing = pricing(), quantity = 10) {
  const send = vi.fn();
  const priceCheck = vi.fn();
  const view = await render(ExtendedFlattenTicketComponent, {
    inputs: { pricing: value, quantity, quoteReceivedAtMs: NOW_MS },
    on: { send, priceCheck },
  });
  return { ...view, send, priceCheck };
}

/** Press Review and let the Clerk answer, as the panel shell does. */
async function review(
  fixture: ComponentFixture<ExtendedFlattenTicketComponent>,
  limitPrice: number,
  evaluation: Partial<SqliteProposedLimitEvaluation> = {},
  quote: Partial<SqliteExtendedLimitPricing> = {},
): Promise<void> {
  fireEvent.click(button('Review limit order'));
  fixture.detectChanges();
  fixture.componentRef.setInput(
    'pricing',
    pricing({ ...quote, proposal: reading(limitPrice, evaluation) }),
  );
  fixture.detectChanges();
  await fixture.whenStable();
  fixture.detectChanges();
}

function limitInput(): HTMLInputElement {
  const input = screen.getByLabelText('Limit price (USD)');
  if (!(input instanceof HTMLInputElement)) throw new Error('limit price is not an input');
  return input;
}

function button(name: string): HTMLElement {
  return screen.getByRole('button', { name });
}

describe('ExtendedFlattenTicketComponent', () => {
  afterEach(() => vi.useRealTimers());

  function atNow(offsetMs = 0): void {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(NOW_MS + offsetMs);
  }

  it('shows the live book, the suggested limit and the furthest price accepted', async () => {
    atNow(2_000);
    await renderTicket();

    expect(screen.getByText('Pre-market')).toBeTruthy();
    expect(screen.getByText(/\$512\.31/)).toBeTruthy();
    expect(screen.getByText(/× 300/)).toBeTruthy();
    expect(screen.getByText(/\$512\.36/)).toBeTruthy();
    expect(screen.getByText(/\$0\.05/)).toBeTruthy();
    expect(screen.getByText(/2 s old/)).toBeTruthy();
    expect(screen.getByText(/bid − 20 bps/)).toBeTruthy();
    expect(screen.getByText(/\$510\.26/)).toBeTruthy();
    expect(limitInput().value).toBe('511.28');
  });

  it('asks the Clerk what the price does rather than working it out here', async () => {
    atNow();
    const { fixture, priceCheck } = await renderTicket();

    fireEvent.click(button('Review limit order'));
    fixture.detectChanges();

    expect(priceCheck).toHaveBeenCalledExactlyOnceWith(511.28);
    // Nothing is claimed about the price until the Clerk has read it.
    expect(screen.getByRole('status').textContent).toContain('Asking the Clerk');

    fixture.componentRef.setInput(
      'pricing',
      pricing({ proposal: reading(511.28, { through_book_bps: 20.1, worst_case_cost: 10.3 }) }),
    );
    fixture.detectChanges();

    expect(screen.getByRole('status').textContent).toContain('20.1 bps below the bid');
    expect(screen.getByRole('status').textContent).toContain('$10.30');
  });

  it('sends exactly the price and quote it was reviewed against, not a later refresh', async () => {
    atNow();
    const { send, fixture } = await renderTicket();

    await review(fixture, 511.28);
    expect(screen.getByText(/Sell 10/)).toBeTruthy();
    expect(screen.getByText(/at limit \$511\.28, extended hours, good for today\./)).toBeTruthy();

    // A refresh lands underneath the open confirmation.
    fixture.componentRef.setInput(
      'pricing',
      pricing({ bid: 512.0, suggested_limit_price: 510.98, quote_observed_at_ms: NOW_MS + 2_000 }),
    );
    fixture.detectChanges();
    expect(screen.getByText(/at limit \$511\.28/)).toBeTruthy();

    fireEvent.click(button('Send limit order'));

    expect(send).toHaveBeenCalledExactlyOnceWith({
      limit_price: 511.28,
      quote_observed_at_ms: NOW_MS,
    });
  });

  it('keeps an edited price across quote refreshes and says it may not fill', async () => {
    atNow();
    const { send, fixture } = await renderTicket();

    fireEvent.input(limitInput(), { target: { value: '512.50' } });
    fixture.componentRef.setInput('pricing', pricing({ suggested_limit_price: 511.37 }));
    fixture.detectChanges();
    expect(limitInput().value).toBe('512.50');

    await review(fixture, 512.5, { resting: true, through_book_bps: -3.7, worst_case_cost: 0 });

    expect(screen.getByRole('status').textContent)
      .toContain('Above the bid: this sells only if a buyer pays your price.');
    fireEvent.click(button('Send limit order'));
    expect(send).toHaveBeenCalledExactlyOnceWith({
      limit_price: 512.5,
      quote_observed_at_ms: NOW_MS,
    });
  });

  it('never confirms a price the Clerk puts past its band', async () => {
    atNow();
    const { fixture, send } = await renderTicket();

    fireEvent.input(limitInput(), { target: { value: '510.25' } });
    fixture.detectChanges();
    await review(fixture, 510.25, { outside_band: true });

    expect(screen.getByRole('status').textContent).toContain("Past the Clerk's band");
    expect(screen.queryByRole('button', { name: 'Send limit order' })).toBeNull();
    expect(send).not.toHaveBeenCalled();
  });

  it('warns when the quantity is deeper than the size shown at the bid', async () => {
    atNow();
    const { fixture } = await renderTicket(pricing({ bid_size: 100 }), 500);

    fireEvent.input(limitInput(), { target: { value: '511.28' } });
    fixture.detectChanges();
    fixture.componentRef.setInput(
      'pricing',
      pricing({ bid_size: 100, proposal: reading(511.28, { thin_book: true }) }),
    );
    fixture.detectChanges();

    expect(screen.getByText(/Only 100 shown at the bid; your 500 may fill further through the book/))
      .toBeTruthy();
  });

  it('warns when the spread is wider than the Clerk says is safe', async () => {
    atNow();
    await renderTicket(pricing({ ask: 516.0, spread: 3.69, spread_bps: 71.8, wide_spread: true }));

    expect(screen.getByText(/The spread is wider than 50 bps/)).toBeTruthy();
  });

  it('cannot be reviewed against a quote this browser received too long ago', async () => {
    atNow(10_001);
    await renderTicket();

    expect(screen.getByText(/more than 10 seconds old/)).toBeTruthy();
    expect(button('Review limit order').hasAttribute('disabled')).toBe(true);
  });

  it('will not send a reviewed order once its quote ages out', async () => {
    atNow();
    const { send, fixture } = await renderTicket();
    await review(fixture, 511.28);

    // Time passes and the next refresh lands, as it does while the ticket is open.
    vi.setSystemTime(NOW_MS + 10_001);
    fixture.componentRef.setInput('pricing', pricing());
    fixture.detectChanges();

    expect(screen.getByText(/Review again at the live quote/)).toBeTruthy();
    expect(button('Send limit order').hasAttribute('disabled')).toBe(true);
    fireEvent.click(button('Send limit order'));
    expect(send).not.toHaveBeenCalled();
  });

  it('will not send a reviewed order after the position changed', async () => {
    atNow();
    const { send, fixture } = await renderTicket();
    await review(fixture, 511.28);

    fixture.componentRef.setInput('quantity', 4);
    fixture.detectChanges();

    expect(screen.getByText(/The position changed after you reviewed/)).toBeTruthy();
    fireEvent.click(button('Send limit order'));
    expect(send).not.toHaveBeenCalled();
  });

  it('refuses a price that is not a plain positive decimal', async () => {
    atNow();
    const { fixture, priceCheck } = await renderTicket();

    fireEvent.input(limitInput(), { target: { value: '1e2' } });
    fixture.detectChanges();

    expect(screen.getByRole('status').textContent).toContain('Enter a positive price');
    expect(button('Review limit order').hasAttribute('disabled')).toBe(true);
    expect(priceCheck).not.toHaveBeenCalled();
  });

  it('moves focus to Send when reviewing and back to Review on cancel', async () => {
    atNow();
    const { fixture } = await renderTicket();

    await review(fixture, 511.28);
    expect(document.activeElement).toBe(button('Send limit order'));

    fireEvent.click(button('Cancel'));
    await fixture.whenStable();
    fixture.detectChanges();
    expect(document.activeElement).toBe(button('Review limit order'));
  });

  it('prices a buy to cover from the ask', async () => {
    atNow();
    const { fixture } = await renderTicket(
      pricing({ side: 'buy', phase: 'POST', suggested_limit_price: 513.39, band_limit_price: 514.41 }),
    );

    expect(screen.getByText('After-hours')).toBeTruthy();
    expect(screen.getByText(/ask \+ 20 bps/)).toBeTruthy();

    fireEvent.click(button('Review limit order'));
    fixture.detectChanges();
    fixture.componentRef.setInput(
      'pricing',
      pricing({
        side: 'buy',
        phase: 'POST',
        suggested_limit_price: 513.39,
        band_limit_price: 514.41,
        proposal: reading(513.39),
      }),
    );
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.getByText(/Buy to cover 10/)).toBeTruthy();
    expect(screen.getByText(/at limit \$513\.39/)).toBeTruthy();
  });
});
