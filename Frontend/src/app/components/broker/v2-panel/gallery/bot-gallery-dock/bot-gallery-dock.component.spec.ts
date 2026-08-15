import { type CdkDragDrop, CdkDropList } from '@angular/cdk/drag-drop';
import { By } from '@angular/platform-browser';
import { fireEvent, render, screen } from '@testing-library/angular';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { loadLayout } from '../lib/gallery-layout';
import type { ChartBar, ChartFillMarker, GalleryBotView, GalleryLiveStatus } from '../lib/gallery.types';
import { BotGalleryDockComponent } from './bot-gallery-dock.component';

// Mounting `<app-bot-tile>` mounts lightweight-charts underneath — mock it
// the same way `bot-tile.component.spec.ts` does; the dock's own behavior
// (layout, reorder, pagination, filter, footer) doesn't touch the chart.
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
    TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 },
  };
});

const ACCOUNT_ID = 'PA3';
const BROKER = 'alpaca';

function bot(overrides: Partial<GalleryBotView> = {}): GalleryBotView {
  return {
    sid: 'sid-1',
    symbol: 'SPY',
    label: 'ORB breakout',
    running: true,
    phase: 'RUNNING',
    desired_state: 'ON_DUTY',
    needs_attention: false,
    realized_pnl_today: 0,
    open_pnl: 0,
    fills_today: 0,
    last_bar_at_ms: null,
    primary_action: { action_id: 'stop', label: 'Stop', enabled: true, disabled_reason: null },
    ...overrides,
  };
}

function bots(n: number): GalleryBotView[] {
  return Array.from({ length: n }, (_, i) =>
    bot({ sid: `sid-${i}`, symbol: `SYM${i}`, label: `Bot ${i}` }));
}

interface RenderInputs {
  bots: GalleryBotView[];
  barsBySymbol?: ReadonlyMap<string, readonly ChartBar[]>;
  markersBySid?: ReadonlyMap<string, readonly ChartFillMarker[]>;
  broker?: string;
  accountId?: string;
  pendingSids?: ReadonlySet<string>;
  status?: GalleryLiveStatus;
}

async function renderDock(inputs: RenderInputs) {
  const onAction = vi.fn();
  const result = await render(BotGalleryDockComponent, {
    inputs: {
      bots: inputs.bots,
      barsBySymbol: inputs.barsBySymbol ?? new Map(),
      markersBySid: inputs.markersBySid ?? new Map(),
      broker: inputs.broker ?? BROKER,
      accountId: inputs.accountId ?? ACCOUNT_ID,
      pendingSids: inputs.pendingSids ?? new Set(),
      status: inputs.status ?? 'live',
    },
    on: { action: onAction },
  });
  return { ...result, onAction };
}

function cellSids(container: HTMLElement): string[] {
  return [...container.querySelectorAll<HTMLElement>('.gallery-dock__cell')]
    .map((el) => el.dataset['sid'] ?? '');
}

describe('BotGalleryDockComponent', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('renders one tile per bot', async () => {
    const { container } = await renderDock({ bots: bots(6) });

    expect(cellSids(container)).toHaveLength(6);
  });

  it('reorders on a CDK drop and persists the new order-only layout', async () => {
    const { container, fixture } = await renderDock({ bots: bots(4) });
    expect(cellSids(container)).toEqual(['sid-0', 'sid-1', 'sid-2', 'sid-3']);

    const dropListDe = fixture.debugElement.query(By.directive(CdkDropList));
    expect(dropListDe).not.toBeNull();
    const dropEvent = { previousIndex: 0, currentIndex: 2 } as unknown as CdkDragDrop<unknown>;
    dropListDe.triggerEventHandler('cdkDropListDropped', dropEvent);
    fixture.detectChanges();

    const expectedOrder = ['sid-1', 'sid-2', 'sid-0', 'sid-3'];
    expect(cellSids(container)).toEqual(expectedOrder);
    expect(loadLayout(ACCOUNT_ID)).toEqual(expectedOrder);
  });

  it('has no resize handle in the DOM — only the pointer-only drag handle remains', async () => {
    const { container } = await renderDock({ bots: bots(1) });

    expect(container.querySelector('.gallery-dock__resize-handle')).toBeNull();
    const dragHandle = container.querySelector('.gallery-dock__drag-handle');
    expect(dragHandle?.getAttribute('tabindex')).toBe('-1');
    expect(dragHandle?.getAttribute('aria-hidden')).toBe('true');
  });

  it('"Reset layout" restores the auto catalog order and clears persistence', async () => {
    const { container, fixture } = await renderDock({ bots: bots(4) });
    const dropListDe = fixture.debugElement.query(By.directive(CdkDropList));
    const dropEvent = { previousIndex: 0, currentIndex: 3 } as unknown as CdkDragDrop<unknown>;
    dropListDe.triggerEventHandler('cdkDropListDropped', dropEvent);
    fixture.detectChanges();
    expect(cellSids(container)).not.toEqual(['sid-0', 'sid-1', 'sid-2', 'sid-3']);

    fireEvent.click(screen.getByRole('button', { name: 'Reset layout' }));
    fixture.detectChanges();

    expect(cellSids(container)).toEqual(['sid-0', 'sid-1', 'sid-2', 'sid-3']);
    expect(loadLayout(ACCOUNT_ID)).toEqual([]);
  });

  it('paginates a roster over 20 bots, showing only the first page', async () => {
    const { container } = await renderDock({ bots: bots(25) });

    expect(cellSids(container)).toHaveLength(20);
    expect(screen.getByText('page 1 of 2')).toBeTruthy();
  });

  it('advances to the next page and back', async () => {
    const { container, fixture } = await renderDock({ bots: bots(25) });

    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    fixture.detectChanges();

    expect(cellSids(container)).toHaveLength(5);
    expect(screen.getByText('page 2 of 2')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Next page' })).toHaveProperty('disabled', true);

    fireEvent.click(screen.getByRole('button', { name: 'Previous page' }));
    fixture.detectChanges();

    expect(cellSids(container)).toHaveLength(20);
    expect(screen.getByRole('button', { name: 'Previous page' })).toHaveProperty('disabled', true);
  });

  it('clamps the page and disables Next when the roster shrinks below the current page', async () => {
    const { fixture } = await renderDock({ bots: bots(25) });

    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    fixture.detectChanges();
    expect(screen.getByText('page 2 of 2')).toBeTruthy();

    fixture.componentRef.setInput('bots', bots(15));
    fixture.detectChanges();

    expect(screen.getByText('page 1 of 1')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Next page' })).toHaveProperty('disabled', true);
  });

  it('reorders within page 2 without corrupting page 1 (page-relative index math)', async () => {
    const { fixture } = await renderDock({ bots: bots(25) });

    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    fixture.detectChanges();

    // Page 2 holds sid-20..sid-24 at page-relative indices 0..4; move the
    // first of those to the end of the page.
    const dropListDe = fixture.debugElement.query(By.directive(CdkDropList));
    const dropEvent = { previousIndex: 0, currentIndex: 4 } as unknown as CdkDragDrop<unknown>;
    dropListDe.triggerEventHandler('cdkDropListDropped', dropEvent);
    fixture.detectChanges();

    const persistedSids = loadLayout(ACCOUNT_ID);
    expect(persistedSids.slice(0, 20)).toEqual(bots(20).map((b) => b.sid));
    expect(persistedSids.slice(20)).toEqual(['sid-21', 'sid-22', 'sid-23', 'sid-24', 'sid-20']);
  });

  it('forwards a tile quick action up through the dock action output', async () => {
    const { onAction } = await renderDock({ bots: bots(1) });

    fireEvent.click(screen.getByRole('button', { name: /^Stop$/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));

    expect(onAction).toHaveBeenCalledWith({ sid: 'sid-0', actionId: 'stop' });
  });

  it('forwards pendingSids down to the matching tile only', async () => {
    await renderDock({ bots: bots(2), pendingSids: new Set(['sid-0']) });

    const buttons = screen.getAllByRole('button', { name: /^Stop…?$/i }) as HTMLButtonElement[];
    const pending = buttons.filter((button) => button.getAttribute('aria-busy') === 'true');
    const notPending = buttons.filter((button) => button.getAttribute('aria-busy') === 'false');

    expect(pending).toHaveLength(1);
    expect(pending[0].disabled).toBe(true);
    expect(notPending).toHaveLength(1);
    expect(notPending[0].disabled).toBe(false);
  });

  describe('status filter', () => {
    function mixedBots(): GalleryBotView[] {
      return [
        bot({ sid: 'sid-0', symbol: 'SYM0', running: true, needs_attention: false }),
        bot({ sid: 'sid-1', symbol: 'SYM1', running: true, needs_attention: true }),
        bot({ sid: 'sid-2', symbol: 'SYM2', running: false, needs_attention: false }),
      ];
    }

    it('shows a live count on every segment, computed off the full unfiltered roster', async () => {
      await renderDock({ bots: mixedBots() });

      expect(screen.getByRole('radio', { name: 'All 3' })).toBeTruthy();
      expect(screen.getByRole('radio', { name: 'Running 2' })).toBeTruthy();
      expect(screen.getByRole('radio', { name: 'Needs attn 1' })).toBeTruthy();
      expect(screen.getByRole('radio', { name: 'Stopped 1' })).toBeTruthy();
    });

    it('defaults to "All", showing every bot', async () => {
      const { container } = await renderDock({ bots: mixedBots() });

      expect(cellSids(container)).toEqual(['sid-0', 'sid-1', 'sid-2']);
      expect(screen.getByRole('radio', { name: 'All 3' }).getAttribute('aria-checked')).toBe('true');
    });

    it('"Running" shows only bots with running === true', async () => {
      const { container } = await renderDock({ bots: mixedBots() });

      fireEvent.click(screen.getByRole('radio', { name: 'Running 2' }));

      expect(cellSids(container)).toEqual(['sid-0', 'sid-1']);
      expect(screen.getByRole('radio', { name: 'Running 2' }).getAttribute('aria-checked')).toBe('true');
      expect(screen.getByRole('radio', { name: 'All 3' }).getAttribute('aria-checked')).toBe('false');
    });

    it('"Needs attn" shows only running bots flagged needs_attention', async () => {
      const { container } = await renderDock({ bots: mixedBots() });

      fireEvent.click(screen.getByRole('radio', { name: 'Needs attn 1' }));

      expect(cellSids(container)).toEqual(['sid-1']);
    });

    it('"Stopped" shows only bots with running === false', async () => {
      const { container } = await renderDock({ bots: mixedBots() });

      fireEvent.click(screen.getByRole('radio', { name: 'Stopped 1' }));

      expect(cellSids(container)).toEqual(['sid-2']);
    });

    it('switching back to "All" restores every bot', async () => {
      const { container } = await renderDock({ bots: mixedBots() });

      fireEvent.click(screen.getByRole('radio', { name: 'Stopped 1' }));
      fireEvent.click(screen.getByRole('radio', { name: 'All 3' }));

      expect(cellSids(container)).toEqual(['sid-0', 'sid-1', 'sid-2']);
    });

    it('shows an honest in-dock empty note when the filter matches nothing, distinct from the whole-wall empty state', async () => {
      const allRunning = [
        bot({ sid: 'sid-0', running: true, needs_attention: false }),
        bot({ sid: 'sid-1', running: true, needs_attention: false }),
      ];
      const { container } = await renderDock({ bots: allRunning });

      fireEvent.click(screen.getByRole('radio', { name: 'Stopped 0' }));

      expect(screen.getByText('No bots match this filter')).toBeTruthy();
      expect(cellSids(container)).toHaveLength(0);
      // The footer (and its filters, so the operator can switch back) stays rendered.
      expect(screen.getByRole('button', { name: 'Reset layout' })).toBeTruthy();
      expect(screen.getByRole('radio', { name: 'All 2' })).toBeTruthy();
    });
  });

  describe('footer', () => {
    it('renders Reset layout, Today · 1m, and a pager alongside the filter', async () => {
      await renderDock({ bots: bots(3) });

      expect(screen.getByRole('button', { name: 'Reset layout' })).toBeTruthy();
      expect(screen.getByText('Today · 1m')).toBeTruthy();
      expect(screen.getByText('page 1 of 1')).toBeTruthy();
    });

    it.each([
      ['live', 'Live'],
      ['stale', 'Delayed'],
      ['connecting', 'Connecting…'],
      ['error', 'Feed error'],
    ] as const)('renders the %s connection status as "%s" in the ●Live indicator', async (status, label) => {
      await renderDock({ bots: bots(1), status });

      expect(screen.getByText(label)).toBeTruthy();
    });
  });
});
