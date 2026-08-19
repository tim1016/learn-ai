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
  countPlottedFills,
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
    LineType: { Simple: 0, WithSteps: 1 },
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
                name: 'ema',
                category: 'trend',
                description: 'Exponential moving average',
                configurable_params: [{
                  name: 'length',
                  type: 'int',
                  default: 10,
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
    chartMocks.supportedIndicators.mockReturnValue(of({ names: ['ema'] }));
    localStorage.removeItem('broker-v2.chart-timezone.v1');
  });

  it('renders the concise Live and 15m Delayed source tabs', async () => {
    await render(DualPaneChartComponent, {
      inputs: { symbol: 'SPY', liveBars: [], histBars: [] },
    });

    expect(screen.getByRole('tab', { name: 'Live' })).toBeTruthy();
    expect(screen.getByRole('tab', { name: '15m Delayed' })).toBeTruthy();
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

  it('renders all Polygon timeframe buttons after switching source', async () => {
    const { fixture } = await render(DualPaneChartComponent, {
      inputs: { symbol: 'SPY', liveBars: [], histBars: [] },
    });

    screen.getByRole('tab', { name: '15m Delayed' }).click();
    fixture.detectChanges();

    for (const timeframe of ['1m', '15m', '30m', '1h', '1D']) {
      expect(screen.getByRole('button', { name: timeframe })).toBeTruthy();
    }
  });

  it('shows when Polygon cannot satisfy the indicator calculation budget', async () => {
    const { fixture } = await render(DualPaneChartComponent, {
      inputs: {
        symbol: 'SPY',
        liveBars: [],
        histBars: [],
        histIndicatorBars: [liveBar(1_700_000_000_000)],
        histIndicatorBarBudget: 2_800,
        histIndicatorBarBudgetSatisfied: false,
      },
    });

    screen.getByRole('tab', { name: '15m Delayed' }).click();
    fixture.detectChanges();

    expect(screen.getByText(/Polygon returned 1 of 2800 requested calculation candles/))
      .toBeTruthy();
  });

  it('emits historyTimeframeChange when a Polygon timeframe is clicked', async () => {
    const onHistoryTimeframeChange = vi.fn();

    const { fixture } = await render(DualPaneChartComponent, {
      inputs: { symbol: 'SPY', liveBars: [], histBars: [], historyTimeframe: '1m' },
      on: { historyTimeframeChange: onHistoryTimeframeChange },
    });

    screen.getByRole('tab', { name: '15m Delayed' }).click();
    fixture.detectChanges();
    screen.getByRole('button', { name: '15m' }).click();
    expect(onHistoryTimeframeChange).toHaveBeenCalledWith('15m');
  });

  it('recalculates active indicators from the newly selected Polygon candle set', async () => {
    const user = userEvent.setup();
    const liveBars = [liveBar(1_700_000_000_000, 1_700_000_060_000)];
    const oneMinuteBars = [liveBar(1_699_999_940_000, 1_700_000_000_000)];
    const fifteenMinuteBars = [liveBar(1_699_999_100_000, 1_700_000_000_000)];
    chartMocks.calculateIndicators.mockImplementation((_symbol, bars: readonly ChartBar[]) => of({
      symbol: 'SPY',
      indicators: [{
        id: 'ema_10',
        panel: 'main',
        type: 'line',
        color: '#FF9800',
        data: [{ t: bars[0].end_ms, value: 101 }],
        refs: [],
      }],
    }));
    const { fixture } = await render(DualPaneChartComponent, {
      inputs: {
        symbol: 'SPY',
        liveBars,
        histBars: oneMinuteBars,
        histIndicatorBars: oneMinuteBars,
        historyDataTimeframe: '1m',
        historyTimeframe: '1m',
      },
    });

    await user.click(screen.getByRole('button', { name: 'Expand market chart' }));
    const rail = screen.getByRole('complementary', { name: 'Indicator picker rail' });
    const trendButtons = within(rail).getAllByRole('button', { name: /trend/i });
    await user.click(trendButtons[trendButtons.length - 1]);
    await user.click(within(rail).getByRole('button', { name: 'Add', hidden: true }));
    await user.click(within(rail).getByRole('button', { name: /add ema to chart/i }));
    await waitFor(() => expect(chartMocks.calculateIndicators).toHaveBeenCalledWith(
      'SPY', liveBars, [expect.objectContaining({ name: 'ema', params: { length: 10 } })],
    ));

    await user.click(screen.getByRole('tab', { name: '15m Delayed' }));
    await waitFor(() => expect(chartMocks.calculateIndicators).toHaveBeenCalledWith(
      'SPY', oneMinuteBars, [expect.objectContaining({ name: 'ema', params: { length: 10 } })],
    ));

    fixture.componentRef.setInput('historyTimeframe', '15m');
    fixture.detectChanges();
    expect(screen.getByText('No candles in this window')).toBeTruthy();

    fixture.componentRef.setInput('histBars', fifteenMinuteBars);
    fixture.componentRef.setInput('histIndicatorBars', fifteenMinuteBars);
    fixture.componentRef.setInput('historyDataTimeframe', '15m');
    fixture.detectChanges();
    await waitFor(() => expect(chartMocks.calculateIndicators).toHaveBeenCalledWith(
      'SPY', fifteenMinuteBars, [expect.objectContaining({ name: 'ema', params: { length: 10 } })],
    ));
  });

  it('calculates Polygon indicators with warmup bars while drawing only display bars', async () => {
    const user = userEvent.setup();
    const warmupBars = [liveBar(1_699_999_880_000, 1_699_999_940_000)];
    const displayBars = [liveBar(1_699_999_940_000, 1_700_000_000_000)];
    const indicatorBars = [...warmupBars, ...displayBars];
    await render(DualPaneChartComponent, {
      inputs: {
        symbol: 'SPY',
        liveBars: [],
        histBars: displayBars,
        histIndicatorBars: indicatorBars,
        historyDataTimeframe: '1m',
        historyTimeframe: '1m',
      },
    });

    await user.click(screen.getByRole('button', { name: 'Expand market chart' }));
    const rail = screen.getByRole('complementary', { name: 'Indicator picker rail' });
    const trendButtons = within(rail).getAllByRole('button', { name: /trend/i });
    await user.click(trendButtons[trendButtons.length - 1]);
    await user.click(within(rail).getByRole('button', { name: 'Add', hidden: true }));
    await user.click(within(rail).getByRole('button', { name: /add ema to chart/i }));
    await user.click(screen.getByRole('tab', { name: '15m Delayed' }));

    await waitFor(() => expect(chartMocks.calculateIndicators).toHaveBeenCalledWith(
      'SPY', indicatorBars, [expect.objectContaining({ name: 'ema' })],
    ));
    expect(chartMocks.setData).toHaveBeenCalledWith([expect.objectContaining({
      time: displayBars[0].start_ms / 1000,
    })]);
  });

  it('does not hide a missing history-indicator contract behind display bars', async () => {
    const user = userEvent.setup();
    const displayBars = [liveBar(1_699_999_940_000, 1_700_000_000_000)];
    const { fixture } = await render(DualPaneChartComponent, {
      inputs: {
        symbol: 'SPY',
        liveBars: [],
        histBars: displayBars,
        histIndicatorBars: [],
        historyDataTimeframe: '1m',
        historyTimeframe: '1m',
      },
    });

    await user.click(screen.getByRole('button', { name: 'Expand market chart' }));
    const rail = screen.getByRole('complementary', { name: 'Indicator picker rail' });
    const trendButtons = within(rail).getAllByRole('button', { name: /trend/i });
    await user.click(trendButtons[trendButtons.length - 1]);
    await user.click(within(rail).getByRole('button', { name: 'Add', hidden: true }));
    await user.click(within(rail).getByRole('button', { name: /add ema to chart/i }));
    await user.click(screen.getByRole('tab', { name: '15m Delayed' }));

    await fixture.whenStable();
    expect(chartMocks.calculateIndicators).not.toHaveBeenCalledWith(
      'SPY', displayBars, [expect.objectContaining({ name: 'ema' })],
    );
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

  it('requires a selected EMA length before adding it to the expanded bot chart', async () => {
    const user = userEvent.setup();
    const bars = [liveBar(1_700_000_000_000, 1_700_000_060_000)];
    chartMocks.calculateIndicators.mockReturnValue(of({
      symbol: 'SPY',
      indicators: [{
        id: 'ema_21',
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

    expect(within(rail).getByRole('spinbutton', { name: /ema length/i })).toBeTruthy();
    expect(chartMocks.calculateIndicators).not.toHaveBeenCalled();

    await user.clear(within(rail).getByRole('spinbutton', { name: /ema length/i }));
    await user.type(within(rail).getByRole('spinbutton', { name: /ema length/i }), '21');
    await user.click(within(rail).getByRole('button', { name: /add ema to chart/i }));

    await waitFor(() => expect(chartMocks.calculateIndicators).toHaveBeenCalledWith(
      'SPY',
      bars,
      [expect.objectContaining({ name: 'ema', params: { length: 21 } })],
    ));
    expect(within(rail).getByRole('button', { name: 'Remove EMA 21' })).toBeTruthy();
    expect(screen.getByRole('list', { name: 'Plotted indicators' }).textContent).toContain('EMA 21');
    const colorInput = within(rail).getByLabelText('Change EMA 21 color') as HTMLInputElement;
    expect(colorInput).toBeTruthy();
    await waitFor(() => expect(chartMocks.addSeries).toHaveBeenCalledWith(
      'LineSeries',
      expect.objectContaining({ color: '#FF9800' }),
      0,
    ));

    colorInput.value = '#2dd4bf';
    colorInput.dispatchEvent(new Event('input', { bubbles: true }));
    fixture.detectChanges();
    await waitFor(() => expect(chartMocks.addSeries).toHaveBeenCalledWith(
      'LineSeries',
      expect.objectContaining({ color: '#2dd4bf' }),
      0,
    ));
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
    await user.click(within(rail).getByRole('button', { name: /add ema to chart/i }));
    firstResponse.next({
      symbol: 'SPY',
      indicators: [{
        id: 'ema_10', panel: 'main', type: 'line', color: '#FF9800',
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

  it('counts grouped fills individually in the plotted tally', () => {
    const startMs = 1_700_000_000_000;
    const bars = [
      {
        start_ms: startMs,
        end_ms: startMs + 60_000,
        open: '100',
        high: '102',
        low: '99',
        close: '101',
        volume: 1_000,
        source: 'ibkr' as const,
      },
    ];
    const fills = [
      {
        filled_at_ms: startMs + 10_000,
        side: 'buy' as const,
        quantity: 1,
        price: 100.5,
        order_ref: 'order-1',
        event_key: 'exec-1',
      },
      {
        filled_at_ms: startMs + 20_000,
        side: 'buy' as const,
        quantity: 2,
        price: 100.75,
        order_ref: 'order-2',
        event_key: 'exec-2',
      },
      {
        filled_at_ms: startMs - 600_000,
        side: 'buy' as const,
        quantity: 3,
        price: 99.5,
        order_ref: 'order-3',
        event_key: 'exec-3',
      },
    ];

    // Two in-window fills collapse into one arrow, so the glyph count is 1 --
    // but two fills are represented and the readout must say so.
    expect(toSeriesMarkers(fills, bars)).toHaveLength(1);
    expect(countPlottedFills(fills, bars)).toBe(2);
  });

  it('condenses same-side fills that share a delayed candle into one readable marker', () => {
    const startMs = 1_700_000_000_000;
    const markers = toSeriesMarkers(
      [
        {
          filled_at_ms: startMs + 60_000,
          side: 'sell',
          quantity: 1,
          price: 101,
          order_ref: 'order-1',
          event_key: 'exec-1',
        },
        {
          filled_at_ms: startMs + 120_000,
          side: 'sell',
          quantity: 2,
          price: 101.5,
          order_ref: 'order-2',
          event_key: 'exec-2',
        },
        {
          filled_at_ms: startMs + 180_000,
          side: 'sell',
          quantity: 1,
          price: 102,
          order_ref: 'order-3',
          event_key: 'exec-3',
        },
      ],
      [
        {
          start_ms: startMs,
          end_ms: startMs + 15 * 60_000,
          open: '100',
          high: '103',
          low: '99',
          close: '102',
          volume: 1_000,
          source: 'polygon',
        },
      ],
    );

    expect(markers).toEqual([
      expect.objectContaining({
        time: startMs / 1_000,
        position: 'aboveBar',
        shape: 'arrowDown',
        text: 'SELL · 3 fills',
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
