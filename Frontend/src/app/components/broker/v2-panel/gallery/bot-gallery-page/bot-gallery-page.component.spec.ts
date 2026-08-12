import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { fireEvent, render, screen } from '@testing-library/angular';
import { MessageService } from 'primeng/api';
import { describe, expect, it, vi } from 'vitest';

import type { ChartBar, ChartFillMarker, PanelAction } from '../../lib/broker-v2-panel.types';
import { BrokerV2PanelService } from '../../lib/broker-v2-panel.service';
import { GalleryLiveStore } from '../lib/gallery-live-store.service';
import type { GalleryBotView, GalleryLiveStatus } from '../lib/gallery.types';
import { BotGalleryPageComponent } from './bot-gallery-page.component';

// Mounting the dock mounts `<app-bot-tile>`, which mounts lightweight-charts
// — mock it the same way `bot-tile.component.spec.ts` does.
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

const BROKER = 'alpaca';
const ACCOUNT_ID = 'PA3';

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

function fakeAction(actionId: 'resume' | 'stop', enabled = true): PanelAction {
  return {
    action_id: actionId,
    label: actionId === 'resume' ? 'Resume' : 'Stop',
    explanation: `${actionId} this bot.`,
    enabled,
    blockers: [],
    confirmation: null,
    revision: 1,
    concurrency_token: `${actionId}-token`,
  };
}

interface FakeGalleryStore {
  bots: ReturnType<typeof signal<GalleryBotView[]>>;
  barsBySymbol: ReturnType<typeof signal<ReadonlyMap<string, readonly ChartBar[]>>>;
  markersBySid: ReturnType<typeof signal<ReadonlyMap<string, readonly ChartFillMarker[]>>>;
  status: ReturnType<typeof signal<GalleryLiveStatus>>;
  start: ReturnType<typeof vi.fn>;
  stop: ReturnType<typeof vi.fn>;
}

function fakeGalleryStore(overrides: {
  bots?: GalleryBotView[];
  status?: GalleryLiveStatus;
} = {}): FakeGalleryStore {
  return {
    bots: signal<GalleryBotView[]>(overrides.bots ?? []),
    barsBySymbol: signal<ReadonlyMap<string, readonly ChartBar[]>>(new Map()),
    markersBySid: signal<ReadonlyMap<string, readonly ChartFillMarker[]>>(new Map()),
    status: signal<GalleryLiveStatus>(overrides.status ?? 'connecting'),
    start: vi.fn().mockResolvedValue(undefined),
    stop: vi.fn(),
  };
}

async function renderPage(store: FakeGalleryStore) {
  const panelService = {
    getPanel: vi.fn().mockResolvedValue({ actions: [fakeAction('stop')] }),
    runBotAction: vi.fn().mockResolvedValue({
      action_id: 'stop',
      applied: true,
      revision: 2,
      concurrency_token: 'next-token',
      message: 'Bot stopped.',
    }),
  };
  const messageService = { add: vi.fn() };

  TestBed.overrideComponent(BotGalleryPageComponent, {
    set: { providers: [{ provide: GalleryLiveStore, useValue: store }] },
  });

  const view = await render(BotGalleryPageComponent, {
    inputs: { broker: BROKER, accountId: ACCOUNT_ID },
    providers: [
      provideRouter([]),
      { provide: BrokerV2PanelService, useValue: panelService },
      { provide: MessageService, useValue: messageService },
    ],
  });

  return { ...view, panelService, messageService };
}

describe('BotGalleryPageComponent', () => {
  it('starts the gallery live store with the routed broker and account', async () => {
    const store = fakeGalleryStore({ status: 'live', bots: [bot()] });

    await renderPage(store);

    expect(store.start).toHaveBeenCalledWith(BROKER, ACCOUNT_ID);
  });

  it('shows a loading skeleton while connecting with no bots yet', async () => {
    const store = fakeGalleryStore({ status: 'connecting', bots: [] });

    await renderPage(store);

    expect(screen.getByLabelText('Loading bot gallery')).toBeTruthy();
    expect(screen.queryByText('No running bots')).toBeNull();
  });

  it('renders the dock once bots are present', async () => {
    const store = fakeGalleryStore({ status: 'live', bots: [bot()] });

    await renderPage(store);

    expect(screen.getByText('SPY')).toBeTruthy();
    expect(screen.queryByLabelText('Loading bot gallery')).toBeNull();
  });

  it('shows the honest empty state with a link to the bots roster when nothing is running', async () => {
    const store = fakeGalleryStore({ status: 'live', bots: [] });

    await renderPage(store);

    expect(screen.getByText('No running bots')).toBeTruthy();
    const link = screen.getByRole('link', { name: 'View bots roster' }) as HTMLAnchorElement;
    expect(link.getAttribute('href')).toBe(`/brokers/${BROKER}/accounts/${ACCOUNT_ID}/bots`);
  });

  it('shows a non-blocking delayed indicator when the feed is stale, and keeps the dock visible', async () => {
    const store = fakeGalleryStore({ status: 'stale', bots: [bot()] });

    await renderPage(store);

    expect(screen.getAllByText('Delayed').length).toBeGreaterThan(0);
    expect(screen.getByText('SPY')).toBeTruthy();
  });

  it('shows an error banner when the feed has never connected', async () => {
    const store = fakeGalleryStore({ status: 'error', bots: [] });

    await renderPage(store);

    expect(screen.getByRole('alert').textContent).toContain('Gallery feed unavailable');
    expect(screen.queryByText('No running bots')).toBeNull();
  });

  it('routes a confirmed tile quick action through getPanel then runBotAction', async () => {
    const store = fakeGalleryStore({ status: 'live', bots: [bot({ sid: 'sid-1' })] });
    const { panelService } = await renderPage(store);

    fireEvent.click(screen.getByRole('button', { name: /^Stop$/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    await Promise.resolve();
    await Promise.resolve();

    expect(panelService.getPanel).toHaveBeenCalledWith(BROKER, ACCOUNT_ID, 'sid-1');
    expect(panelService.runBotAction).toHaveBeenCalledWith(
      BROKER,
      ACCOUNT_ID,
      'sid-1',
      fakeAction('stop'),
    );
  });

  it('does not call runBotAction when the refreshed panel no longer offers the action', async () => {
    const store = fakeGalleryStore({ status: 'live', bots: [bot({ sid: 'sid-1' })] });
    const { panelService } = await renderPage(store);
    panelService.getPanel.mockResolvedValue({ actions: [fakeAction('stop', false)] });

    fireEvent.click(screen.getByRole('button', { name: /^Stop$/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    await Promise.resolve();
    await Promise.resolve();

    expect(panelService.runBotAction).not.toHaveBeenCalled();
  });

  it('stops the live store when the page is destroyed', async () => {
    const store = fakeGalleryStore({ status: 'live', bots: [bot()] });
    const { fixture } = await renderPage(store);

    fixture.destroy();

    expect(store.stop).toHaveBeenCalled();
  });
});
