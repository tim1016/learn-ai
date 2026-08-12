import { Router } from '@angular/router';
import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { ChartBar, GalleryBotView } from '../lib/gallery.types';
import { BotTileComponent } from './bot-tile.component';

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
