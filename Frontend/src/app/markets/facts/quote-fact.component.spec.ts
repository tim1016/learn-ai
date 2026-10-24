import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { formatTimestampDisplay } from '../../shared/timestamp';
import type { FactState } from './facts-board';
import { QuoteFactComponent } from './quote-fact.component';
import { CLOSE_MS, OBSERVED_MS, UNAVAILABLE, facts, quote } from './testing/facts-fixtures';

const ready = (...args: Parameters<typeof facts>): FactState => ({ kind: 'ready', facts: facts(...args) });
const text = (element: HTMLElement) => (element.textContent ?? '').replace(/\s+/g, ' ').trim();
const asOf = (ms: number) => formatTimestampDisplay(ms, { mode: 'local' });

describe('QuoteFactComponent', () => {
  it('names the live quote, the instant it was observed and the close the day move is measured from', async () => {
    await render(QuoteFactComponent, {
      inputs: { state: ready('SPY', quote({ basis: 'live_quote', not_used: [] })) },
    });

    expect(screen.getByText('$759.97')).toBeTruthy();
    expect(screen.getByText(/-6\.92 \(-0\.90%\)/)).toBeTruthy();
    const source = screen.getByText(/Live quote, as of/);
    expect(text(source)).toBe(
      `Live quote, as of ${asOf(OBSERVED_MS)} · day move from the ${formatTimestampDisplay(CLOSE_MS, { mode: 'date-et', granularity: 'short-date' })} close of $766.89`,
    );
    expect(screen.queryByText(/^Not live/)).toBeNull();
  });

  it('says a delayed snapshot is not live, and why, so the label is never a bare "last"', async () => {
    await render(QuoteFactComponent, { inputs: { state: ready('SPY') } });

    expect(screen.getByText(/Delayed snapshot, as of/)).toBeTruthy();
    expect(screen.getByText(/^Not live: No live line is streaming SPY/)).toBeTruthy();
  });

  it('names the last completed close with the instant of that close', async () => {
    await render(QuoteFactComponent, {
      inputs: { state: ready('SPY', quote({ basis: 'last_close', as_of_ms: CLOSE_MS })) },
    });

    const source = screen.getByText(/Last completed close, as of/);
    expect(text(source)).toContain(`as of ${asOf(CLOSE_MS)}`);
  });

  it('keeps the price and says why there is no move when no close could be measured from', async () => {
    await render(QuoteFactComponent, {
      inputs: { state: ready('SPY', quote({ move: null, move_unavailable_reason: 'The lake holds no regular-hours bars for SPY on 2026-10-22.' })) },
    });

    expect(screen.getByText('$759.97')).toBeTruthy();
    expect(screen.getByText('Move unavailable')).toBeTruthy();
    expect(screen.getByText('The lake holds no regular-hours bars for SPY on 2026-10-22.')).toBeTruthy();
    expect(screen.queryByText(/day move from the/)).toBeNull();
  });

  it('says unavailable for the price and the move when no source answered, with each source\'s reason', async () => {
    await render(QuoteFactComponent, { inputs: { state: ready('SPY', UNAVAILABLE) } });

    expect(screen.getByText('Price unavailable')).toBeTruthy();
    expect(screen.getByText('Move unavailable')).toBeTruthy();
    expect(screen.getByText('Delayed snapshot: The vendor snapshot could not be read (ConnectionError).')).toBeTruthy();
    expect(screen.queryByText(/^\$/)).toBeNull();
  });

  it('says unavailable when the whole read failed, and asks to read again only on request', async () => {
    const { fixture } = await render(QuoteFactComponent, { inputs: { state: { kind: 'failed', message: 'The data plane is down.' } satisfies FactState } });
    const asked: string[] = [];
    fixture.componentInstance.retry.subscribe(() => asked.push('retry'));

    expect(screen.getByText('Price unavailable')).toBeTruthy();
    expect(screen.getByText('The data plane is down.')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Read the price again' }));
    expect(asked).toEqual(['retry']);
  });

  it('shows no price while the read is in flight, and no figure that could be mistaken for one', async () => {
    await render(QuoteFactComponent, { inputs: { state: { kind: 'loading' } satisfies FactState } });

    expect(screen.getByText('Reading the price…')).toBeTruthy();
    expect(screen.queryByText(/^\$/)).toBeNull();
    expect(screen.queryByText(/unavailable/)).toBeNull();
  });

  it('draws a row\'s price and its source line without the longer notes', async () => {
    await render(QuoteFactComponent, { inputs: { state: ready('SPY'), layout: 'row' } });

    expect(screen.getByText(/Delayed snapshot, as of/)).toBeTruthy();
    expect(screen.queryByText(/^Not live/)).toBeNull();
  });
});
