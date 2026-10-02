/** #2639: the bot page's strategy view — read on load, re-read when the live
 * panel shows a newer decision, a failed read kept to the chart panel, and one
 * candle selection shared with the decisions list. Kept apart from
 * `bot-panel-shell.component.spec.ts` so that file's shard stays in budget. */
import { HttpErrorResponse } from '@angular/common/http';
import { type ComponentFixture } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { render, screen, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { MessageService } from 'primeng/api';
import { afterAll, beforeEach, describe, expect, it, vi } from 'vitest';

import { provideFleetDirectory, testLane } from '../../../../fleet/fleet-directory-testing';
import { BrokersService } from '../../../../services/brokers.service';
import { formatTimestampDisplay } from '../../../../shared/timestamp/timestamp-display';
import { fakeBotPanelView, fakeChartFeed, panelRefusalBody } from '../../../../testing/bot-panel-fixtures';
import { barCloseMs, fakeRecentDecision, fakeStrategyView } from '../../../../testing/strategy-view-fixtures';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import type { BotPanelLiveSnapshot, BotPanelView, RecentDecisionView } from '../lib/broker-v2-panel.types';
import type { StrategyChartOverlay } from '../strategy-view/strategy-chart-overlay';
import { STRATEGY_CHART_FACTORY } from '../strategy-view/strategy-chart.component';
import { BotPanelShellComponent } from './bot-panel-shell.component';

vi.mock('lightweight-charts', () => ({
  createSeriesMarkers: vi.fn().mockReturnValue({ setMarkers: vi.fn() }),
  CandlestickSeries: 'CandlestickSeries',
  LineSeries: 'LineSeries',
  TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 },
}));

interface MockChart {
  readonly candles: { setData: ReturnType<typeof vi.fn>; attachPrimitive: ReturnType<typeof vi.fn> };
  readonly click: (time: number) => void;
}

const charts: MockChart[] = [];

function createMockChart(): object {
  let onClick: ((param: unknown) => void) | null = null;
  const candles = { setData: vi.fn(), attachPrimitive: vi.fn() };
  charts.push({ candles, click: (time) => onClick?.({ time, point: { x: 1, y: 1 } }) });
  return {
    addSeries: vi.fn((type: string) => (type === 'CandlestickSeries'
      ? candles
      : { setData: vi.fn(), attachPrimitive: vi.fn(), createPriceLine: vi.fn() })),
    removeSeries: vi.fn(),
    timeScale: () => ({
      fitContent: vi.fn(), logicalToCoordinate: vi.fn(), width: () => 800,
      subscribeSizeChange: vi.fn(), unsubscribeSizeChange: vi.fn(),
    }),
    panes: () => [{ setStretchFactor: vi.fn() }, { setStretchFactor: vi.fn() }],
    applyOptions: vi.fn(),
    subscribeClick: vi.fn((handler: (param: unknown) => void) => { onClick = handler; }),
    unsubscribeClick: vi.fn(),
    remove: vi.fn(),
  };
}

class StubEventSource {
  static latest: StubEventSource | null = null;
  addEventListener = vi.fn();
  close = vi.fn();

  constructor(readonly url: string) {
    StubEventSource.latest = this;
  }

  emit(name: string, data: string): void {
    for (const [eventName, listener] of this.addEventListener.mock.calls) {
      if (eventName === name) (listener as (event: MessageEvent<string>) => void)(new MessageEvent(name, { data }));
    }
  }
}

const originalEventSource = globalThis.EventSource;
(globalThis as { EventSource?: unknown }).EventSource = StubEventSource;

const RUN_DECISIONS: RecentDecisionView[] = [fakeRecentDecision(12, 3), fakeRecentDecision(11, 2)];

function panel(decisions: readonly RecentDecisionView[] = RUN_DECISIONS): BotPanelView {
  return fakeBotPanelView({ strategy_instance_id: 'sid-001', account_id: 'DUM284968', recent_decisions: [...decisions] });
}

function snapshot(version: number, decisions: readonly RecentDecisionView[] = RUN_DECISIONS): BotPanelLiveSnapshot {
  return {
    stream_epoch: 'test-epoch',
    surface_version: version,
    panel: panel(decisions),
    live_chart: {
      strategy_instance_id: 'sid-001',
      symbol: 'SPY',
      trading_date_open_ms: 1_759_239_000_000,
      trading_date_close_ms: 1_759_262_400_000,
      resolution: '5s',
      bars: [],
      fill_markers: [],
      overlay_notices: [],
      feed: fakeChartFeed(),
      as_of_ms: 1_759_239_000_000,
    },
  };
}

function service(getStrategyView: ReturnType<typeof vi.fn>) {
  return {
    getPanelProfile: vi.fn().mockResolvedValue({ broker: 'alpaca', fee_fidelity: 'none', live_bars_supported: false, stations: [] }),
    getLiveSnapshot: vi.fn().mockResolvedValue(snapshot(1)),
    liveStreamUrl: vi.fn().mockReturnValue('/api/test/live-stream'),
    getCurrentRun: vi.fn().mockRejectedValue(new Error('No current run fixture.')),
    getBudget: vi.fn().mockRejectedValue(new Error('No budget fixture.')),
    getHistoryChart: vi.fn().mockRejectedValue(new Error('No history fixture.')),
    getStrategyView,
  };
}

async function settle(fixture: ComponentFixture<BotPanelShellComponent>): Promise<void> {
  for (let step = 0; step < 3; step += 1) {
    await fixture.whenStable();
    fixture.detectChanges();
  }
}

async function renderPage(getStrategyView: ReturnType<typeof vi.fn>) {
  const { fixture } = await render(BotPanelShellComponent, {
    inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
    providers: [
      provideRouter([]),
      provideFleetDirectory({ observed_at_ms: 1_757_000_000_000, clerks: [testLane({ clerk_id: 'clrk_spec' })] }),
      { provide: STRATEGY_CHART_FACTORY, useValue: createMockChart },
      { provide: BrokerV2PanelService, useValue: service(getStrategyView) },
      { provide: BrokersService, useValue: { checkSqliteRecoveryAction: vi.fn() } },
      { provide: MessageService, useValue: { add: vi.fn() } },
    ],
  });
  await settle(fixture);
  return fixture;
}

function decisionRow(barIndex: number): HTMLElement {
  const minute = formatTimestampDisplay(barCloseMs(barIndex), { mode: 'local', granularity: 'minute' });
  const row = within(screen.getByRole('list', { name: 'Recent decisions' })).getAllByRole('listitem')
    .find((item) => item.textContent?.includes(minute));
  if (row === undefined) throw new Error(`No decision row for bar ${barIndex}.`);
  return row;
}

describe('BotPanelShellComponent — strategy view (#2639)', () => {
  beforeEach(() => {
    charts.length = 0;
    localStorage.clear();
  });

  afterAll(() => {
    globalThis.EventSource = originalEventSource;
  });

  it('re-reads the strategy view when the live panel lists a newer decision, keeping the chart meanwhile', async () => {
    const getStrategyView = vi.fn(() => Promise.resolve(fakeStrategyView()));
    const fixture = await renderPage(getStrategyView);

    expect(getStrategyView).toHaveBeenCalledTimes(1);
    expect(getStrategyView).toHaveBeenCalledWith(
      expect.objectContaining({ broker: 'alpaca', clerkId: 'clrk_spec', accountId: 'DUM284968' }),
      'sid-001',
    );
    expect(screen.getByRole('tab', { name: 'Strategy · 15m', selected: true })).toBeTruthy();

    // A refresh with the same newest decision reads nothing new.
    StubEventSource.latest?.emit('snapshot', JSON.stringify(snapshot(2)));
    await settle(fixture);
    expect(getStrategyView).toHaveBeenCalledTimes(1);

    StubEventSource.latest?.emit('snapshot', JSON.stringify(snapshot(3, [fakeRecentDecision(13, 3), ...RUN_DECISIONS])));
    await settle(fixture);
    expect(getStrategyView).toHaveBeenCalledTimes(2);
    // The same chart redrew the new read; it was never torn down for a blank one.
    expect(charts).toHaveLength(1);
    expect(charts[0].candles.setData.mock.calls.length).toBeGreaterThan(1);
  });

  it('keeps a failed read to the chart panel, in the backend’s words, and retries on request', async () => {
    const user = userEvent.setup();
    const getStrategyView = vi.fn().mockRejectedValueOnce(new HttpErrorResponse({
      status: 503,
      error: panelRefusalBody({
        message: 'This bot’s decision record is unavailable.',
        why: 'The Clerk could not read its decision receipts.',
        next_action: null,
      }),
    })).mockResolvedValue(fakeStrategyView());
    const fixture = await renderPage(getStrategyView);

    const chartPanel = screen.getByRole('region', { name: 'Chart for SPY' });
    const alert = within(chartPanel).getByRole('alert');
    expect(within(alert).getByText('This bot’s decision record is unavailable.')).toBeTruthy();
    expect(within(alert).getByText('The Clerk could not read its decision receipts.')).toBeTruthy();
    // The rest of the page is untouched.
    expect(screen.getByRole('heading', { name: 'Recent decisions' })).toBeTruthy();
    expect(decisionRow(3)).toBeTruthy();

    await user.click(within(alert).getByRole('button', { name: 'Retry strategy view' }));
    await settle(fixture);

    expect(getStrategyView).toHaveBeenCalledTimes(2);
    expect(within(chartPanel).queryByRole('alert')).toBeNull();
    expect(within(chartPanel).getByRole('img', { name: /decision candles for SPY/ })).toBeTruthy();
  });

  it('shares one candle selection between the strategy chart and the decisions list', async () => {
    const user = userEvent.setup();
    const fixture = await renderPage(vi.fn().mockResolvedValue(fakeStrategyView()));
    const overlay = charts[0].candles.attachPrimitive.mock.calls[0][0] as StrategyChartOverlay;

    charts[0].click(barCloseMs(2) / 1000);
    await settle(fixture);
    expect(decisionRow(2).getAttribute('aria-current')).toBe('true');

    await user.click(within(decisionRow(3)).getByRole('button'));
    await settle(fixture);
    expect(overlay.current().highlightCloseMs).toBe(barCloseMs(3));
    expect(decisionRow(2).getAttribute('aria-current')).toBeNull();
  });
});
