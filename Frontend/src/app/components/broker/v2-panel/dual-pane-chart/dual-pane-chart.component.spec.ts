import { render, screen } from '@testing-library/angular';
import { beforeEach, describe, it, expect, vi } from 'vitest';
import {
  DualPaneChartComponent,
  toSeriesMarkers,
} from './dual-pane-chart.component';

const chartMocks = vi.hoisted(() => ({
  setMarkers: vi.fn(),
  setData: vi.fn(),
  fitContent: vi.fn(),
}));

// Mock lightweight-charts — the actual DOM chart is not exercised in unit tests.
vi.mock('lightweight-charts', () => {
  const mockTimeScale = { fitContent: chartMocks.fitContent };
  const createMockSeries = () => ({
    setData: chartMocks.setData,
    applyOptions: vi.fn(),
  });
  const createSeriesMarkers = vi.fn().mockReturnValue({
    setMarkers: chartMocks.setMarkers,
  });
  const createMockChart = () => ({
    addSeries: vi.fn().mockReturnValue(createMockSeries()),
    removeSeries: vi.fn(),
    timeScale: vi.fn().mockReturnValue(mockTimeScale),
    applyOptions: vi.fn(),
    remove: vi.fn(),
  });
  return {
    createChart: vi.fn().mockImplementation(() => createMockChart()),
    createSeriesMarkers,
    CandlestickSeries: 'CandlestickSeries',
  };
});


describe('DualPaneChartComponent', () => {
  beforeEach(() => {
    chartMocks.setMarkers.mockClear();
    chartMocks.setData.mockClear();
    chartMocks.fitContent.mockClear();
  });

  it('renders source tabs for IBKR live and Polygon', async () => {
    await render(DualPaneChartComponent, {
      inputs: { symbol: 'SPY', liveBars: [], histBars: [] },
    });

    expect(screen.getByRole('tab', { name: /IBKR live/i })).toBeTruthy();
    expect(screen.getByRole('tab', { name: /Polygon/i })).toBeTruthy();
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
    await fixture.whenStable();
    chartMocks.setData.mockClear();

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

    expect(chartMocks.setData).toHaveBeenLastCalledWith([
      {
        time: 1_700_000_000,
        open: 100,
        high: 102,
        low: 99,
        close: 101,
      },
    ]);
  });

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
    const fitCount = chartMocks.fitContent.mock.calls.length;

    fixture.componentRef.setInput('liveBars', [
      initialBar,
      {
        ...initialBar,
        start_ms: initialBar.end_ms,
        end_ms: initialBar.end_ms + 5_000,
      },
    ]);
    fixture.detectChanges();

    expect(chartMocks.fitContent).toHaveBeenCalledTimes(fitCount);
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
