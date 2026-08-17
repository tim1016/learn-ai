import { Router } from '@angular/router';
import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { ChartBar, ChartFillMarker, GalleryBotView } from '../lib/gallery.types';
import { BotTileComponent } from './bot-tile.component';

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
    day_pnl: 85.25,
    session_change_pct: null,
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

function fillMarker(overrides: Partial<ChartFillMarker> = {}): ChartFillMarker {
  return {
    filled_at_ms: 1_700_000_030_000,
    side: 'buy',
    quantity: 2,
    price: 101,
    order_ref: 'order-1',
    event_key: 'exec-1',
    ...overrides,
  };
}

function routerProvider(navigate = vi.fn().mockResolvedValue(true)) {
  return { provide: Router, useValue: { navigate } };
}

describe('BotTileComponent', () => {
  it('renders the header identity and strategy label', async () => {
    const { container } = await render(BotTileComponent, {
      inputs: {
        bot: bot(),
        bars: [bar()],
        broker: 'alpaca',
        accountId: 'PA3',
      },
      providers: [routerProvider()],
    });

    expect(screen.getByText('SPY')).toBeTruthy();
    expect(container.querySelector('app-asset-identity')?.textContent).toContain('SPY');
    expect(screen.getByText('ORB breakout')).toBeTruthy();
  });

  it('keeps the gallery asset identity compact inside the 24px header', async () => {
    const { container } = await render(BotTileComponent, {
      inputs: {
        bot: bot(),
        bars: [bar()],
        broker: 'alpaca',
        accountId: 'PA3',
      },
      providers: [routerProvider()],
    });

    const identity = container.querySelector('app-asset-identity');
    if (!(identity instanceof HTMLElement)) throw new Error('expected asset identity to render');
    expect(identity.classList.contains('asset-identity--xs')).toBe(true);
    expect(getComputedStyle(identity).getPropertyValue('--asset-identity-logo-size').trim()).toBe('16px');
  });

  it('colours the legend delta positive on an up session', async () => {
    await render(BotTileComponent, {
      inputs: {
        bot: bot({ session_change_pct: 0.1 }),
        bars: [
          bar({ start_ms: 1_700_000_000_000, open: '100.00', close: '100.00' }),
          bar({ start_ms: 1_700_000_060_000, open: '100.00', close: '110.00' }),
        ],
        broker: 'alpaca',
        accountId: 'PA3',
      },
      providers: [routerProvider()],
    });

    const delta = screen.getByText('+10.00%');
    expect(delta.classList.contains('bot-tile__legend-item--positive')).toBe(true);
  });

  it('colours the legend delta negative on a down session', async () => {
    await render(BotTileComponent, {
      inputs: {
        bot: bot({ session_change_pct: -0.1 }),
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
    expect(delta.classList.contains('bot-tile__legend-item--negative')).toBe(true);
  });

  it('renders the default legend (Δ%/fills/P&L) and mounts the canvas without throwing when there are no bars yet', async () => {
    await render(BotTileComponent, {
      inputs: { bot: bot(), bars: [], broker: 'alpaca', accountId: 'PA3' },
      providers: [routerProvider()],
    });

    expect(screen.getByText('—')).toBeTruthy(); // Δ% — fewer than 1 bar
    expect(screen.getByText('3')).toBeTruthy(); // fills_today
    expect(screen.getByText('+$85.25')).toBeTruthy(); // realized_pnl_today + open_pnl
  });

  it('repaints when bars change after the canvas context mounts', async () => {
    const clearRect = vi.fn();
    const context = new Proxy(
      {
        clearRect,
        measureText: (text: string) => ({ width: text.length * 6 }),
      },
      {
        get: (target, property) => (property in target ? target[property as keyof typeof target] : () => undefined),
        set: () => true,
      },
    ) as unknown as CanvasRenderingContext2D;
    const getContext = vi
      .spyOn(HTMLCanvasElement.prototype, 'getContext')
      .mockImplementation(((kind: string) => (kind === '2d' ? context : null)) as HTMLCanvasElement['getContext']);

    try {
      const { fixture } = await render(BotTileComponent, {
        inputs: {
          bot: bot(),
          bars: [bar()],
          broker: 'alpaca',
          accountId: 'PA3',
        },
        providers: [routerProvider()],
      });
      await waitFor(() => expect(clearRect).toHaveBeenCalled());
      const paintsAfterMount = clearRect.mock.calls.length;

      fixture.componentRef.setInput('bars', [bar(), bar({ start_ms: 1_700_000_060_000, end_ms: 1_700_000_120_000 })]);
      fixture.detectChanges();

      await waitFor(() => expect(clearRect.mock.calls.length).toBeGreaterThan(paintsAfterMount));
    } finally {
      getContext.mockRestore();
    }
  });

  it('swaps the legend to the hovered bar\'s OHLCV (with a matching fill) on mousemove, and reverts on mouseleave', async () => {
    const hoveredBar = bar({
      start_ms: 1_700_000_060_000,
      end_ms: 1_700_000_120_000,
      open: '100.00',
      high: '105.00',
      low: '95.00',
      close: '102.50',
      volume: 4_200,
    });
    const { container } = await render(BotTileComponent, {
      inputs: {
        bot: bot({ session_change_pct: 0.025 }),
        bars: [bar({ start_ms: 1_700_000_000_000, end_ms: 1_700_000_060_000 }), hoveredBar],
        markers: [fillMarker({ filled_at_ms: hoveredBar.start_ms + 1_000, side: 'buy', quantity: 2, price: 101.5 })],
        broker: 'alpaca',
        accountId: 'PA3',
      },
      providers: [routerProvider()],
    });

    // Default legend, before any hover.
    expect(screen.getByText('+2.50%')).toBeTruthy();

    const chartRegion = container.querySelector('.bot-tile__chart') as Element;
    // x=200 lands on the second bar with the default (unsized-by-ResizeObserver)
    // renderer config: plot.left=4, plot.width=312, barCount=2 -> barWidth=156.
    fireEvent.mouseMove(chartRegion, { clientX: 200 });

    await waitFor(() => {
      const legendText = container.querySelector('.bot-tile__legend-text')?.textContent ?? '';
      expect(legendText).toContain('O100.00');
      expect(legendText).toContain('H105.00');
      expect(legendText).toContain('L95.00');
      expect(legendText).toContain('C102.50');
      expect(legendText).toContain('V4,200');
      expect(legendText).toContain('BUY 2 @ 101.5');
    });
    expect(screen.queryByText('+2.50%')).toBeNull();

    fireEvent.mouseLeave(chartRegion);

    await waitFor(() => {
      expect(container.querySelector('.bot-tile__legend-text')).toBeNull();
    });
    expect(screen.getByText('+2.50%')).toBeTruthy();
  });

  it('carries needs_attention as a text hint on the chart region, not colour alone', async () => {
    const { container } = await render(BotTileComponent, {
      inputs: {
        bot: bot({ running: true, needs_attention: true }),
        bars: [bar()],
        broker: 'alpaca',
        accountId: 'PA3',
      },
      providers: [routerProvider()],
    });

    expect(container.querySelector('.bot-tile__chart')?.getAttribute('aria-label')).toBe(
      'Open SPY · sid-1 detail — needs attention',
    );
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
    expect(button.querySelector('.pi-spinner')).not.toBeNull();
    expect(button.textContent?.trim()).toBe('');
  });

  it('keeps the quick action actionable when not pending', async () => {
    const { container } = await render(BotTileComponent, {
      inputs: { bot: bot(), bars: [bar()], broker: 'alpaca', accountId: 'PA3', pending: false },
      providers: [routerProvider()],
    });

    const button = screen.getByRole('button', { name: /^Stop$/i }) as HTMLButtonElement;
    expect(button.disabled).toBe(false);
    expect(button.getAttribute('aria-busy')).toBe('false');
    expect(button.querySelector('.pi-stop')).not.toBeNull();
    expect(button.textContent?.trim()).toBe('');
    expect(container.querySelector('.bot-tile__header .bot-tile__action-button')).toBe(button);
    expect(container.querySelector('.bot-tile__footer')).toBeNull();
    expect(screen.queryByText('Realized')).toBeNull();
    expect(screen.queryByText('Open')).toBeNull();
    expect(screen.queryByText('Fills')).toBeNull();
  });

  it('uses a play icon for the Resume action', async () => {
    await render(BotTileComponent, {
      inputs: {
        bot: bot({ primary_action: { action_id: 'resume', label: 'Resume', enabled: true, disabled_reason: null } }),
        bars: [bar()],
        broker: 'alpaca',
        accountId: 'PA3',
      },
      providers: [routerProvider()],
    });

    expect(screen.getByRole('button', { name: 'Resume' }).querySelector('.pi-play')).not.toBeNull();
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

  it('opens an inline confirm and emits resume for a stopped bot', async () => {
    const onAction = vi.fn();
    await render(BotTileComponent, {
      inputs: {
        bot: bot({
          running: false,
          primary_action: { action_id: 'resume', label: 'Resume', enabled: true, disabled_reason: null },
        }),
        bars: [bar()],
        broker: 'alpaca',
        accountId: 'PA3',
      },
      on: { action: onAction },
      providers: [routerProvider()],
    });

    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
    expect(onAction).not.toHaveBeenCalled();
    expect(screen.getByText('Resume SPY · sid-1?')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));

    expect(onAction).toHaveBeenCalledWith({ sid: 'sid-1', actionId: 'resume' });
    expect(screen.queryByText('Resume SPY · sid-1?')).toBeNull();
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
