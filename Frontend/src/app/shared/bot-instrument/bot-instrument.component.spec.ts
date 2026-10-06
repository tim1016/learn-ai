import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { BotInstrumentComponent, orderSymbol, type TradedInstrument } from './bot-instrument.component';

const QQQ: TradedInstrument = { kind: 'stock', symbol: 'QQQ' };

describe('orderSymbol', () => {
  it('is what the bot trades, which is what it listens to unless the server says otherwise', () => {
    expect(orderSymbol('SPY', QQQ)).toBe('QQQ');
    expect(orderSymbol('SPY', null)).toBe('SPY');
    expect(orderSymbol('SPY', undefined)).toBe('SPY');
  });
});

describe('BotInstrumentComponent', () => {
  it('shows a bot that trades what it listens to as that symbol alone', async () => {
    const { container, fixture } = await render(BotInstrumentComponent, { inputs: { symbol: 'SPY' } });
    const host = fixture.nativeElement as HTMLElement;

    expect(screen.getByText('SPY')).toBeTruthy();
    expect(container.textContent).not.toContain('→');
    expect(host.getAttribute('aria-label')).toBeNull();
    expect(screen.queryByRole('group')).toBeNull();
  });

  it('shows a bot that trades something else as an arrow line, named in full words', async () => {
    const { container } = await render(BotInstrumentComponent, { inputs: { symbol: 'SPY', traded: QQQ } });

    const line = screen.getByRole('group', { name: 'Listens to SPY, trades QQQ' });
    expect(line.getAttribute('title')).toBe('Listens to SPY, trades QQQ');
    expect(screen.getByText('SPY')).toBeTruthy();
    expect(screen.getByText('QQQ')).toBeTruthy();
    const arrow = container.querySelector('.bot-instrument__arrow');
    expect(arrow?.textContent).toBe('→');
    expect(arrow?.getAttribute('aria-hidden')).toBe('true');
  });

  it('says it in words where the page has room', async () => {
    const { container } = await render(BotInstrumentComponent, {
      inputs: { symbol: 'SPY', traded: QQQ, form: 'words' },
    });

    expect(screen.getByText('Listens to')).toBeTruthy();
    expect(screen.getByText('Trades')).toBeTruthy();
    expect(container.textContent).not.toContain('→');
  });

  it('never works out the answer from the tickers: the same symbol traded as something else still shows both', async () => {
    await render(BotInstrumentComponent, {
      inputs: { symbol: 'SPY', traded: { kind: 'option', symbol: 'SPY' } },
    });

    expect(screen.getByRole('group', { name: 'Listens to SPY, trades SPY' })).toBeTruthy();
    expect(screen.getAllByText('SPY')).toHaveLength(2);
  });
});
