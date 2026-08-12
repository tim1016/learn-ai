import { Router } from '@angular/router';
import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { ChartBar, ChartFillMarker, GalleryBotView } from '../lib/gallery.types';
import { BotTileComponent, toTileMarkers, toVolumeBar } from './bot-tile.component';

// Mock lightweight-charts — the actual DOM chart is not exercised in unit
// tests (see dual-pane-chart.component.spec.ts for the grounding pattern).
vi.mock('lightweight-charts', () => {
  const createMockSeries = () => ({
    setData: vi.fn(),
    applyOptions: vi.fn(),
    priceScale: vi.fn().mockReturnValue({ applyOptions: vi.fn() }),
  });
  const createSeriesMarkers = vi.fn().mockReturnValue({ setMarkers: vi.fn() });
  const createMockChart = () => ({
    addSeries: vi.fn().mockReturnValue(createMockSeries()),
    timeScale: vi.fn().mockReturnValue({ fitContent: vi.fn() }),
    remove: vi.fn(),
  });
  return {
    createChart: vi.fn().mockImplementation(() => createMockChart()),
    createSeriesMarkers,
    CandlestickSeries: 'CandlestickSeries',
    HistogramSeries: 'HistogramSeries',
  };
});

function bar(overrides: Partial<ChartBar> = {}): ChartBar {
  return {
    start_ms: 1_700_000_000_000,
    end_ms: 1_700_000_060_000,
    open: '100.00',
    high: '101.00',
    low: '99.00',
    close: '100.50',
    volume: 1_000,
    source: 'ibkr',
    ...overrides,
  };
}

function bot(overrides: Partial<GalleryBotView> = {}): GalleryBotView {
  return {
    sid: 'sid-1',
    symbol: 'SPY',
    label: 'ORB breakout',
    running: true,
    phase: 'RUNNING',
    desired_state: 'ON_DUTY',
    needs_attention: false,
    realized_pnl_today: 125.5,
    open_pnl: -40.25,
    fills_today: 3,
    last_bar_at_ms: 1_700_000_060_000,
    primary_action: {
      action_id: 'stop',
      label: 'Stop',
      enabled: true,
      disabled_reason: null,
    },
    ...overrides,
  };
}

function routerProvider(navigate = vi.fn().mockResolvedValue(true)) {
  return { provide: Router, useValue: { navigate } };
}

describe('BotTileComponent', () => {
  it('renders the header identity, live price, and a green delta on an up day', async () => {
    await render(BotTileComponent, {
      inputs: {
        bot: bot(),
        bars: [
          bar({ start_ms: 1_700_000_000_000, open: '100.00', close: '100.00' }),
          bar({ start_ms: 1_700_000_060_000, open: '100.00', close: '110.00' }),
        ],
        broker: 'alpaca',
        accountId: 'PA3',
      },
      providers: [routerProvider()],
    });

    expect(screen.getByText('SPY')).toBeTruthy();
    expect(screen.getByText('ORB breakout')).toBeTruthy();
    expect(screen.getByText('$110.00')).toBeTruthy();
    const delta = screen.getByText('+10.00%');
    expect(delta.classList.contains('bot-tile__delta--positive')).toBe(true);
  });

  it('renders a red delta on a down day', async () => {
    await render(BotTileComponent, {
      inputs: {
        bot: bot(),
        bars: [
          bar({ start_ms: 1_700_000_000_000, open: '100.00', close: '100.00' }),
          bar({ start_ms: 1_700_000_060_000, open: '100.00', close: '90.00' }),
        ],
        broker: 'alpaca',
        accountId: 'PA3',
      },
      providers: [routerProvider()],
    });

    const delta = screen.getByText('-10.00%');
    expect(delta.classList.contains('bot-tile__delta--negative')).toBe(true);
  });

  it('shows a placeholder price and delta when there are no bars yet', async () => {
    await render(BotTileComponent, {
      inputs: { bot: bot(), bars: [], broker: 'alpaca', accountId: 'PA3' },
      providers: [routerProvider()],
    });

    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
  });

  it('renders footer realized/open P&L colored by sign, and the fills count', async () => {
    await render(BotTileComponent, {
      inputs: {
        bot: bot({ realized_pnl_today: 125.5, open_pnl: -40.25, fills_today: 3 }),
        bars: [bar()],
        broker: 'alpaca',
        accountId: 'PA3',
      },
      providers: [routerProvider()],
    });

    const realized = screen.getByText('+$125.50');
    expect(realized.classList.contains('pnl--positive')).toBe(true);
    const open = screen.getByText('-$40.25');
    expect(open.classList.contains('pnl--negative')).toBe(true);
    expect(screen.getByText('3')).toBeTruthy();
  });

  it('renders a dash and neutral tone for unavailable P&L/fills, instead of a fabricated zero', async () => {
    const { container } = await render(BotTileComponent, {
      inputs: {
        bot: bot({ realized_pnl_today: null, open_pnl: null, fills_today: null }),
        bars: [bar()],
        broker: 'alpaca',
        accountId: 'PA3',
      },
      providers: [routerProvider()],
    });

    const [realized, open, fills] = Array.from(
      container.querySelectorAll('.bot-tile__metric strong'),
    );
    expect(realized.textContent?.trim()).toBe('—');
    expect(realized.classList.contains('pnl--neutral')).toBe(true);
    expect(open.textContent?.trim()).toBe('—');
    expect(open.classList.contains('pnl--neutral')).toBe(true);
    expect(fills.textContent?.trim()).toBe('—');
  });

  it('marks the state dot running when the bot is running', async () => {
    const { container } = await render(BotTileComponent, {
      inputs: { bot: bot({ running: true }), bars: [bar()], broker: 'alpaca', accountId: 'PA3' },
      providers: [routerProvider()],
    });
    expect(
      container.querySelector('.bot-tile__dot')?.classList.contains('bot-tile__dot--running'),
    ).toBe(true);
  });

  it('does not mark the state dot running when the bot is stopped', async () => {
    const { container } = await render(BotTileComponent, {
      inputs: { bot: bot({ running: false }), bars: [bar()], broker: 'alpaca', accountId: 'PA3' },
      providers: [routerProvider()],
    });
    expect(
      container.querySelector('.bot-tile__dot')?.classList.contains('bot-tile__dot--running'),
    ).toBe(false);
  });

  it('disables the quick action and surfaces the reason when not enabled', async () => {
    await render(BotTileComponent, {
      inputs: {
        bot: bot({
          primary_action: {
            action_id: 'resume',
            label: 'Resume',
            enabled: false,
            disabled_reason: 'Recovery required before resuming.',
          },
        }),
        bars: [bar()],
        broker: 'alpaca',
        accountId: 'PA3',
      },
      providers: [routerProvider()],
    });

    const button = screen.getByRole('button', { name: /Resume/i }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.title).toBe('Recovery required before resuming.');
  });

  it('disables the quick action and marks it aria-busy when pending', async () => {
    await render(BotTileComponent, {
      inputs: { bot: bot(), bars: [bar()], broker: 'alpaca', accountId: 'PA3', pending: true },
      providers: [routerProvider()],
    });

    const button = screen.getByRole('button', { name: /Stop/i }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.getAttribute('aria-busy')).toBe('true');
    expect(button.textContent?.trim()).toBe('Stop…');
  });

  it('keeps the quick action actionable when not pending', async () => {
    await render(BotTileComponent, {
      inputs: { bot: bot(), bars: [bar()], broker: 'alpaca', accountId: 'PA3', pending: false },
      providers: [routerProvider()],
    });

    const button = screen.getByRole('button', { name: /^Stop$/i }) as HTMLButtonElement;
    expect(button.disabled).toBe(false);
    expect(button.getAttribute('aria-busy')).toBe('false');
  });

  it('opens an inline confirm on quick-action click and only emits action after confirming', async () => {
    const onAction = vi.fn();
    await render(BotTileComponent, {
      inputs: { bot: bot(), bars: [bar()], broker: 'alpaca', accountId: 'PA3' },
      on: { action: onAction },
      providers: [routerProvider()],
    });

    fireEvent.click(screen.getByRole('button', { name: /^Stop$/i }));
    expect(onAction).not.toHaveBeenCalled();
    expect(screen.getByText('Stop SPY · sid-1?')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));

    expect(onAction).toHaveBeenCalledWith({ sid: 'sid-1', actionId: 'stop' });
    expect(screen.queryByText('Stop SPY · sid-1?')).toBeNull();
  });

  it('does not emit when the inline confirm is cancelled', async () => {
    const onAction = vi.fn();
    await render(BotTileComponent, {
      inputs: { bot: bot(), bars: [bar()], broker: 'alpaca', accountId: 'PA3' },
      on: { action: onAction },
      providers: [routerProvider()],
    });

    fireEvent.click(screen.getByRole('button', { name: /^Stop$/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(onAction).not.toHaveBeenCalled();
    expect(screen.queryByText('Stop SPY · sid-1?')).toBeNull();
  });

  it('moves keyboard focus onto the confirm Cancel button when it opens', async () => {
    await render(BotTileComponent, {
      inputs: { bot: bot(), bars: [bar()], broker: 'alpaca', accountId: 'PA3' },
      providers: [routerProvider()],
    });

    fireEvent.click(screen.getByRole('button', { name: /^Stop$/i }));

    await waitFor(() => {
      expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Cancel' }));
    });
  });

  it('cancels the confirm on Escape without emitting', async () => {
    const onAction = vi.fn();
    await render(BotTileComponent, {
      inputs: { bot: bot(), bars: [bar()], broker: 'alpaca', accountId: 'PA3' },
      on: { action: onAction },
      providers: [routerProvider()],
    });

    fireEvent.click(screen.getByRole('button', { name: /^Stop$/i }));
    expect(screen.getByText('Stop SPY · sid-1?')).toBeTruthy();

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(onAction).not.toHaveBeenCalled();
    expect(screen.queryByText('Stop SPY · sid-1?')).toBeNull();
  });

  it('returns keyboard focus to the action button after cancelling the confirm', async () => {
    await render(BotTileComponent, {
      inputs: { bot: bot(), bars: [bar()], broker: 'alpaca', accountId: 'PA3' },
      providers: [routerProvider()],
    });

    const actionButton = screen.getByRole('button', { name: /^Stop$/i });
    fireEvent.click(actionButton);
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    await waitFor(() => {
      expect(document.activeElement).toBe(actionButton);
    });
  });

  it('returns keyboard focus to the action button after cancelling via Escape', async () => {
    await render(BotTileComponent, {
      inputs: { bot: bot(), bars: [bar()], broker: 'alpaca', accountId: 'PA3' },
      providers: [routerProvider()],
    });

    const actionButton = screen.getByRole('button', { name: /^Stop$/i });
    fireEvent.click(actionButton);
    fireEvent.keyDown(document, { key: 'Escape' });

    await waitFor(() => {
      expect(document.activeElement).toBe(actionButton);
    });
  });

  it('does not open the confirm when the quick action is disabled', async () => {
    const onAction = vi.fn();
    await render(BotTileComponent, {
      inputs: {
        bot: bot({
          primary_action: { action_id: 'stop', label: 'Stop', enabled: false, disabled_reason: 'Already flat.' },
        }),
        bars: [bar()],
        broker: 'alpaca',
        accountId: 'PA3',
      },
      on: { action: onAction },
      providers: [routerProvider()],
    });

    fireEvent.click(screen.getByRole('button', { name: /Stop/i }));

    expect(onAction).not.toHaveBeenCalled();
    expect(screen.queryByText(/Stop SPY/)).toBeNull();
  });

  it('navigates to the bot detail page when the chart body is clicked', async () => {
    const navigate = vi.fn().mockResolvedValue(true);
    const { container } = await render(BotTileComponent, {
      inputs: { bot: bot(), bars: [bar()], broker: 'alpaca', accountId: 'PA3' },
      providers: [routerProvider(navigate)],
    });

    const chartRegion = container.querySelector('.bot-tile__chart');
    expect(chartRegion).not.toBeNull();
    fireEvent.click(chartRegion as Element);

    expect(navigate).toHaveBeenCalledWith([
      '/brokers', 'alpaca', 'accounts', 'PA3', 'bots', 'sid-1',
    ]);
  });

  it('does not navigate when the quick action is clicked', async () => {
    const navigate = vi.fn().mockResolvedValue(true);
    await render(BotTileComponent, {
      inputs: { bot: bot(), bars: [bar()], broker: 'alpaca', accountId: 'PA3' },
      providers: [routerProvider(navigate)],
    });

    fireEvent.click(screen.getByRole('button', { name: /^Stop$/i }));

    expect(navigate).not.toHaveBeenCalled();
  });
});

describe('toVolumeBar', () => {
  it('floors start_ms to seconds, keeps the raw volume, and colors an up bar green', () => {
    expect(toVolumeBar(bar({ open: '100', close: '105', volume: 500 }))).toEqual({
      time: 1_700_000_000,
      value: 500,
      color: '#26a69a',
    });
  });

  it('colors a down bar red', () => {
    expect(toVolumeBar(bar({ open: '105', close: '100', volume: 500 }))).toEqual({
      time: 1_700_000_000,
      value: 500,
      color: '#ef5350',
    });
  });
});

describe('toTileMarkers', () => {
  function fillMarker(overrides: Partial<ChartFillMarker> = {}): ChartFillMarker {
    return {
      filled_at_ms: 1_700_000_030_000,
      side: 'buy',
      quantity: 2,
      price: 101,
      order_ref: 'order-1',
      ...overrides,
    };
  }

  it('maps a buy fill to a green up-arrow below the bar, at the floored second', () => {
    const [marker] = toTileMarkers([fillMarker()]);

    expect(marker).toEqual({
      time: 1_700_000_030,
      position: 'belowBar',
      color: '#26a69a',
      shape: 'arrowUp',
      text: 'BUY 2 @ 101',
    });
  });

  it('maps a sell fill to a red down-arrow above the bar', () => {
    const [marker] = toTileMarkers([
      fillMarker({ filled_at_ms: 1_700_000_045_000, side: 'sell', quantity: 1, price: 99.5, order_ref: 'order-2' }),
    ]);

    expect(marker).toEqual({
      time: 1_700_000_045,
      position: 'aboveBar',
      color: '#ef5350',
      shape: 'arrowDown',
      text: 'SELL 1 @ 99.5',
    });
  });
});
