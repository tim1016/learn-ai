import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { render, screen, waitFor, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { of, Subject } from 'rxjs';
import { beforeEach, describe, it, expect, vi } from 'vitest';
import {
  DUAL_PANE_CHART_FACTORY,
  DualPaneChartComponent,
  formatChartCrosshairTime,
  toSeriesMarkers,
} from './dual-pane-chart.component';
import type { ChartBar } from '../lib/broker-v2-panel.types';
import { IndicatorCatalogService } from '../../../../shared/indicator-catalog/indicator-catalog.service';
import { BotChartIndicatorService } from './bot-chart-indicator.service';
import type { ChartIndicatorBatchResponse } from './dual-pane-chart-indicators';

const chartMocks = vi.hoisted(() => ({
  createChart: vi.fn(),
  setMarkers: vi.fn(),
  setData: vi.fn(),
  update: vi.fn(),
  fitContent: vi.fn(),
  addSeries: vi.fn(),
  calculateIndicators: vi.fn(),
  supportedIndicators: vi.fn(),
}));

// Mock lightweight-charts — the actual DOM chart is not exercised in unit tests.
vi.mock('lightweight-charts', () => {
  const createSeriesMarkers = vi.fn().mockReturnValue({
    setMarkers: chartMocks.setMarkers,
  });
  return {
    createChart: chartMocks.createChart,
    createSeriesMarkers,
    AreaSeries: 'AreaSeries',
    CandlestickSeries: 'CandlestickSeries',
    HistogramSeries: 'HistogramSeries',
    LineSeries: 'LineSeries',
    TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 },
  };
});

function createMockChart(): object {
  const mockTimeScale = { fitContent: chartMocks.fitContent };
  const createMockSeries = () => ({
    setData: chartMocks.setData,
    update: chartMocks.update,
    applyOptions: vi.fn(),
    createPriceLine: vi.fn(),
  });
  chartMocks.addSeries.mockImplementation(() => createMockSeries());
  return {
    addSeries: chartMocks.addSeries,
    removeSeries: vi.fn(),
    timeScale: vi.fn().mockReturnValue(mockTimeScale),
    applyOptions: vi.fn(),
    remove: vi.fn(),
  };
}

interface ChartHarness {
  chart: {
    timeScale: () => { fitContent: ReturnType<typeof vi.fn> };
    applyOptions: ReturnType<typeof vi.fn>;
  } | null;
  series: {
    setData: ReturnType<typeof vi.fn>;
    update: ReturnType<typeof vi.fn>;
  } | null;
}

function chartHarness(component: DualPaneChartComponent): ChartHarness {
  return component as unknown as ChartHarness;
}

function liveBar(startMs: number, endMs = startMs + 5_000): ChartBar {
  return {
    start_ms: startMs,
    end_ms: endMs,
    open: '100',
    high: '102',
    low: '99',
    close: '101',
    volume: 100,
    source: 'ibkr',
  };
}

describe('DualPaneChartComponent', () => {
  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        {
          provide: DUAL_PANE_CHART_FACTORY,
          useValue: chartMocks.createChart.mockImplementation(() => createMockChart()),
        },
        {
          provide: IndicatorCatalogService,
          useValue: {
            load: vi.fn().mockResolvedValue(undefined),
            categories: signal([{
              name: 'trend',
              indicators: [{
                name: 'sma',
                category: 'trend',
                description: 'Simple moving average',
                configurable_params: [{
                  name: 'length',
                  type: 'int',
                  default: 2,
                  min: 1,
                  max: 200,
                  description: 'Lookback length',
                }],
              }],
            }]),
            loading: signal(false),
          },
        },
        {
          provide: BotChartIndicatorService,
          useValue: {
            calculate: chartMocks.calculateIndicators,
            supportedIndicators: chartMocks.supportedIndicators,
          },
        },
      ],
    });
    chartMocks.createChart.mockClear();
    chartMocks.setMarkers.mockClear();
    chartMocks.setData.mockClear();
    chartMocks.update.mockClear();
    chartMocks.fitContent.mockClear();
    chartMocks.addSeries.mockReset();
    chartMocks.calculateIndicators.mockReset();
    chartMocks.calculateIndicators.mockReturnValue(of({ symbol: 'SPY', indicators: [] }));
    chartMocks.supportedIndicators.mockReset();
    chartMocks.supportedIndicators.mockReturnValue(of({ names: ['sma'] }));
    localStorage.removeItem('broker-v2.chart-timezone.v1');
  });

  it('renders source tabs for IBKR live and Polygon', async () => {
    await render(DualPaneChartComponent, {
      inputs: { symbol: 'SPY', liveBars: [], histBars: [] },
    });

    expect(screen.getByRole('tab', { name: /IBKR live/i })).toBeTruthy();
    expect(screen.getByRole('tab', { name: /Polygon/i })).toBeTruthy();
  });

  it('uses the shared asset identity for the chart symbol', async () => {
    const { container } = await render(DualPaneChartComponent, {
      inputs: {
        symbol: 'NVDA',
        tickerQuote: { ticker: 'NVDA', price: 181.42, changePercent: 1.35 },
        liveBars: [],
        histBars: [],
      },
    });

    await screen.findByText('$181.42');
    const identity = container.querySelector('app-asset-identity');
    expect(identity).not.toBeNull();
    expect(screen.getByText('NVDA')).toBeTruthy();
    expect(identity?.querySelector('img')?.getAttribute('src')).toContain('/nvidia.svg');
  });

  it('shows overlay notice when liveNotices are provided', async () => {
    const notice = {
      code: 'LIVE_UNAVAILABLE',
      message: 'Live feed unavailable — showing Polygon (delayed).',
    };

    await render(DualPaneChartComponent, {
      inputs: {
        symbol: 'SPY',
        liveBars: [],
        histBars: [],
        liveNotices: [notice],
      },
    });

    expect(
      screen.getByText('Live feed unavailable — showing Polygon (delayed).'),
    ).toBeTruthy();
  });

  it('renders all 6 Polygon range buttons after switching source', async () => {
    const { fixture } = await render(DualPaneChartComponent, {
      inputs: { symbol: 'SPY', liveBars: [], histBars: [] },
    });

    screen.getByRole('tab', { name: /Polygon/i }).click();
    fixture.detectChanges();

    for (const preset of ['1D', '5D', '1M', '3M', '1Y', 'All']) {
      expect(screen.getByRole('button', { name: preset })).toBeTruthy();
    }
  });

  it('emits presetChange when a history preset is clicked', async () => {
    const onPresetChange = vi.fn();

    const { fixture } = await render(DualPaneChartComponent, {
      inputs: { symbol: 'SPY', liveBars: [], histBars: [], selectedPreset: '1D' },
      on: { presetChange: onPresetChange },
    });

    screen.getByRole('tab', { name: /Polygon/i }).click();
    fixture.detectChanges();
    screen.getByRole('button', { name: '1M' }).click();
    expect(onPresetChange).toHaveBeenCalledWith('1M');
  });

  it('emits a live resolution change when 1m is selected', async () => {
    const onResolutionChange = vi.fn();

    await render(DualPaneChartComponent, {
      inputs: { symbol: 'SPY', liveBars: [], histBars: [], liveResolution: '5s' },
      on: { liveResolutionChange: onResolutionChange },
    });

    screen.getByRole('button', { name: '1m' }).click();
    expect(onResolutionChange).toHaveBeenCalledWith('1m');
  });

  it('shows one expand button for the shared market canvas', async () => {
    await render(DualPaneChartComponent, {
      inputs: { symbol: 'SPY', liveBars: [], histBars: [] },
    });

    expect(
      screen.getByRole('button', { name: /expand market chart/i }),
    ).toBeTruthy();
  });

  it('restores the indicator picker rail when the market chart is expanded', async () => {
    const user = userEvent.setup();
    const { fixture } = await render(DualPaneChartComponent, {
      inputs: { symbol: 'SPY', liveBars: [], histBars: [] },
    });

    expect(screen.queryByRole('complementary', { name: 'Indicator picker rail' })).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Expand market chart' }));
    fixture.detectChanges();

    const rail = screen.getByRole('complementary', { name: 'Indicator picker rail' });
    expect(within(rail).getByText('Active')).toBeTruthy();
    expect(within(rail).getByText('Indicators')).toBeTruthy();
  });

  it('adds an indicator from the restored rail using the visible candle set', async () => {
    const user = userEvent.setup();
    const bars = [liveBar(1_700_000_000_000, 1_700_000_060_000)];
    chartMocks.calculateIndicators.mockReturnValue(of({
      symbol: 'SPY',
      indicators: [{
        id: 'sma_2',
        panel: 'main',
        type: 'line',
        color: '#FF9800',
        data: [{ t: 1_700_000_060_000, value: 101 }],
        refs: [],
      }],
    }));
    const { fixture } = await render(DualPaneChartComponent, {
      inputs: { symbol: 'SPY', liveBars: bars, histBars: [] },
    });
    await user.click(screen.getByRole('button', { name: 'Expand market chart' }));
    fixture.detectChanges();
    const rail = screen.getByRole('complementary', { name: 'Indicator picker rail' });
    const trendButtons = within(rail).getAllByRole('button', { name: /trend/i });
    await user.click(trendButtons[trendButtons.length - 1]);
    fixture.detectChanges();
    await user.click(within(rail).getByRole('button', { name: 'Add', hidden: true }));
    fixture.detectChanges();

    await waitFor(() => expect(chartMocks.calculateIndicators).toHaveBeenCalledWith(
      'SPY',
      bars,
      [expect.objectContaining({ name: 'sma', params: { length: 2 } })],
    ));
    expect(within(rail).getByRole('button', { name: 'Remove SMA 2' })).toBeTruthy();
    await waitFor(() => expect(chartMocks.addSeries).toHaveBeenCalledWith(
      'LineSeries',
      expect.objectContaining({ color: '#FF9800' }),
      0,
    ));
    expect(within(rail).getByRole<HTMLButtonElement>(
      'button',
      { name: 'Added', hidden: true },
    ).disabled).toBe(true);
  });

  it('keeps the last indicator series visible while refreshed candles are recalculated', async () => {
    const user = userEvent.setup();
    const firstResponse = new Subject<ChartIndicatorBatchResponse>();
    const secondResponse = new Subject<ChartIndicatorBatchResponse>();
    chartMocks.calculateIndicators
      .mockReturnValueOnce(firstResponse.asObservable())
      .mockReturnValueOnce(secondResponse.asObservable());
    const firstBars = [liveBar(1_700_000_000_000, 1_700_000_060_000)];
    const { fixture } = await render(DualPaneChartComponent, {
      inputs: { symbol: 'SPY', liveBars: firstBars, histBars: [] },
    });
    await user.click(screen.getByRole('button', { name: 'Expand market chart' }));
    const rail = screen.getByRole('complementary', { name: 'Indicator picker rail' });
    const trendButtons = within(rail).getAllByRole('button', { name: /trend/i });
    await user.click(trendButtons[trendButtons.length - 1]);
    await user.click(within(rail).getByRole('button', { name: 'Add', hidden: true }));
    firstResponse.next({
      symbol: 'SPY',
      indicators: [{
        id: 'sma_2', panel: 'main', type: 'line', color: '#FF9800',
        data: [{ t: 1_700_000_060_000, value: 101 }], refs: [],
      }],
    });
    firstResponse.complete();
    fixture.detectChanges();
    await waitFor(() => expect(chartMocks.addSeries).toHaveBeenCalledWith(
      'LineSeries', expect.any(Object), 0,
    ));
    const lineAddsBeforeRefresh = chartMocks.addSeries.mock.calls
      .filter(([seriesType]) => seriesType === 'LineSeries').length;

    fixture.componentRef.setInput('liveBars', [
      ...firstBars,
      liveBar(1_700_000_060_000, 1_700_000_120_000),
    ]);
    fixture.detectChanges();

    await waitFor(() => expect(chartMocks.calculateIndicators).toHaveBeenCalledTimes(2));
    expect(chartMocks.addSeries.mock.calls
      .filter(([seriesType]) => seriesType === 'LineSeries').length).toBeGreaterThan(
      lineAddsBeforeRefresh,
    );
  });

  it('defaults chart labels to local time and persists an explicit ET choice', async () => {
    const user = userEvent.setup();
    await render(DualPaneChartComponent, {
      inputs: { symbol: 'SPY', liveBars: [], histBars: [] },
    });

    expect(screen.getByRole('button', { name: 'Local' }).getAttribute('aria-pressed')).toBe('true');
    await user.click(screen.getByRole('button', { name: 'ET' }));
    expect(screen.getByRole('button', { name: 'ET' }).getAttribute('aria-pressed')).toBe('true');
    expect(localStorage.getItem('broker-v2.chart-timezone.v1')).toBe('et');
  });

  it('formats exchange-time labels with America/New_York rather than a fixed offset', () => {
    const seconds = 1_741_524_000;
    const expected = new Intl.DateTimeFormat(undefined, {
      month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
      hour12: false, timeZone: 'America/New_York',
    }).format(new Date(seconds * 1_000));

    expect(formatChartCrosshairTime(seconds, 'et')).toBe(expected);
  });

  it('keeps consecutive five-second candles distinguishable in the time readout', () => {
    const fiveSecondsLater = 1_741_524_005;

    expect(formatChartCrosshairTime(1_741_524_000, 'et')).not.toBe(
      formatChartCrosshairTime(fiveSecondsLater, 'et'),
    );
  });

  it('keeps existing candles visible while a background refresh is loading', async () => {
    await render(DualPaneChartComponent, {
      inputs: {
        symbol: 'SPY',
        liveLoading: true,
        liveBars: [
          {
            start_ms: 1_700_000_000_000,
            end_ms: 1_700_000_005_000,
            open: '100',
            high: '102',
            low: '99',
            close: '101',
            volume: 100,
            source: 'ibkr',
          },
        ],
        histBars: [],
      },
    });

    expect(screen.getByText('1 candle')).toBeTruthy();
    expect(screen.queryByText(/Loading 5s IBKR candles/i)).toBeNull();
  });

  it('pushes bars that arrive after the chart instance is mounted', async () => {
    const { fixture } = await render(DualPaneChartComponent, {
      inputs: { symbol: 'SPY', liveBars: [], histBars: [] },
    });
    await waitFor(
      () => expect(chartHarness(fixture.componentInstance).series).not.toBeNull(),
      { timeout: 5_000 },
    );
    const series = chartHarness(fixture.componentInstance).series;
    if (series === null) throw new Error('chart series did not mount');
    series.setData.mockClear();

    fixture.componentRef.setInput('liveBars', [
      {
        start_ms: 1_700_000_000_000,
        end_ms: 1_700_000_005_000,
        open: '100',
        high: '102',
        low: '99',
        close: '101',
        volume: 100,
        source: 'ibkr',
      },
    ]);
    fixture.detectChanges();
    await fixture.whenStable();

    await waitFor(() => {
      expect(series.setData).toHaveBeenLastCalledWith([
        {
          time: 1_700_000_000,
          open: 100,
          high: 102,
          low: 99,
          close: 101,
        },
      ]);
    });
  }, 15_000);

  it('preserves a manual zoom while fresh bars append to the same view', async () => {
    const initialBar = {
      start_ms: 1_700_000_000_000,
      end_ms: 1_700_000_005_000,
      open: '100',
      high: '102',
      low: '99',
      close: '101',
      volume: 100,
      source: 'ibkr' as const,
    };
    const { fixture } = await render(DualPaneChartComponent, {
      inputs: { symbol: 'SPY', liveBars: [initialBar], histBars: [] },
    });
    await waitFor(
      () => expect(chartHarness(fixture.componentInstance).series).not.toBeNull(),
      { timeout: 5_000 },
    );
    const { chart, series } = chartHarness(fixture.componentInstance);
    if (chart === null || series === null) throw new Error('chart did not mount');
    await waitFor(() => expect(series.setData).toHaveBeenCalled());
    const fitContent = chart.timeScale().fitContent;
    const fitCount = fitContent.mock.calls.length;

    fixture.componentRef.setInput('liveBars', [
      initialBar,
      {
        ...initialBar,
        start_ms: initialBar.end_ms,
        end_ms: initialBar.end_ms + 5_000,
      },
    ]);
    fixture.detectChanges();

    await waitFor(() => {
      expect(series.update).toHaveBeenLastCalledWith(
        expect.objectContaining({ time: 1_700_000_005 }),
      );
    });

    expect(series.setData).toHaveBeenCalledTimes(1);
    expect(fitContent).toHaveBeenCalledTimes(fitCount);
  }, 15_000);

  it.each([
    {
      name: 'a later appended bar is out of order',
      appended: [
        liveBar(1_700_000_005_000),
        liveBar(1_700_000_004_000),
      ],
    },
    {
      name: 'a later appended bar has a forward gap',
      appended: [
        liveBar(1_700_000_005_000),
        liveBar(1_700_000_011_000),
      ],
    },
  ])('replaces chart data when $name', async ({ appended }) => {
    const initial = liveBar(1_700_000_000_000);
    const { fixture } = await render(DualPaneChartComponent, {
      inputs: { symbol: 'SPY', liveBars: [initial], histBars: [] },
    });
    await waitFor(
      () => expect(chartHarness(fixture.componentInstance).series).not.toBeNull(),
      { timeout: 5_000 },
    );
    const series = chartHarness(fixture.componentInstance).series;
    if (series === null) throw new Error('chart series did not mount');
    await waitFor(() => expect(series.setData).toHaveBeenCalled());
    series.setData.mockClear();
    series.update.mockClear();

    fixture.componentRef.setInput('liveBars', [initial, ...appended]);
    fixture.detectChanges();

    await waitFor(() => expect(series.setData).toHaveBeenCalledOnce());
    expect(series.update).not.toHaveBeenCalled();
  });

  it('projects live fills into candle-series markers', () => {
    const markers = toSeriesMarkers(
      [
        {
          filled_at_ms: 1_700_000_030_000,
          side: 'buy',
          quantity: 2,
          price: 101,
          order_ref: 'order-1',
          event_key: 'exec-1',
        },
      ],
      [
        {
          start_ms: 1_700_000_000_000,
          end_ms: 1_700_000_060_000,
          open: '100',
          high: '102',
          low: '99',
          close: '101',
          volume: 1_000,
          source: 'ibkr',
        },
      ],
    );

    expect(markers).toEqual([
      expect.objectContaining({
        time: 1_700_000_000,
        position: 'belowBar',
        shape: 'arrowUp',
        text: 'BUY 2 @ 101',
      }),
    ]);
  });

  it('anchors a fill to its exact five-second candle', () => {
    const base = 1_700_000_000_000;
    const bars = [0, 5_000, 10_000].map((offset) => ({
      start_ms: base + offset,
      end_ms: base + offset + 5_000,
      open: '100',
      high: '102',
      low: '99',
      close: '101',
      volume: 100,
      source: 'ibkr' as const,
    }));

    const [marker] = toSeriesMarkers([
      {
        filled_at_ms: base + 7_250,
        side: 'sell',
        quantity: 1,
        price: 101.25,
        order_ref: 'order-2',
        event_key: 'exec-2',
      },
    ], bars);

    expect(marker.time).toBe((base + 5_000) / 1_000);
    expect(marker.position).toBe('aboveBar');
  });

  it('does not fabricate a position for a fill outside the available candles', () => {
    const markers = toSeriesMarkers(
      [
        {
          filled_at_ms: 1_699_999_000_000,
          side: 'buy',
          quantity: 1,
          price: 99,
          order_ref: 'before-buffer',
          event_key: 'exec-before-buffer',
        },
      ],
      [
        {
          start_ms: 1_700_000_000_000,
          end_ms: 1_700_000_005_000,
          open: '100',
          high: '102',
          low: '99',
          close: '101',
          volume: 100,
          source: 'ibkr',
        },
      ],
    );

    expect(markers).toEqual([]);
  });
});
