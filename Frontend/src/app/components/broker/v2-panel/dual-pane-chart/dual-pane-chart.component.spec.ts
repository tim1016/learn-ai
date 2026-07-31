import { render, screen } from '@testing-library/angular';
import { describe, it, expect, vi } from 'vitest';
import { createSeriesMarkers } from 'lightweight-charts';
import { DualPaneChartComponent } from './dual-pane-chart.component';

// Mock lightweight-charts — the actual DOM chart is not exercised in unit tests.
vi.mock('lightweight-charts', () => {
  const mockTimeScale = { fitContent: vi.fn() };
  const createMockSeries = () => ({ setData: vi.fn(), applyOptions: vi.fn() });
  const createSeriesMarkers = vi.fn().mockReturnValue({ setMarkers: vi.fn() });
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
  it('renders LIVE and HISTORY pane regions', async () => {
    await render(DualPaneChartComponent, {
      inputs: { symbol: 'SPY', liveBars: [], histBars: [] },
    });

    expect(screen.getByRole('region', { name: /live chart for SPY/i })).toBeTruthy();
    expect(screen.getByRole('region', { name: /history chart for SPY/i })).toBeTruthy();
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

  it('renders all 6 history preset buttons', async () => {
    await render(DualPaneChartComponent, {
      inputs: { symbol: 'SPY', liveBars: [], histBars: [] },
    });

    for (const preset of ['1D', '5D', '1M', '3M', '1Y', 'All']) {
      expect(screen.getByRole('button', { name: preset })).toBeTruthy();
    }
  });

  it('emits presetChange when a history preset is clicked', async () => {
    const onPresetChange = vi.fn();

    await render(DualPaneChartComponent, {
      inputs: { symbol: 'SPY', liveBars: [], histBars: [], selectedPreset: '1D' },
      on: { presetChange: onPresetChange },
    });

    screen.getByRole('button', { name: '1M' }).click();
    expect(onPresetChange).toHaveBeenCalledWith('1M');
  });

  it('shows fullscreen expand buttons for both panes', async () => {
    await render(DualPaneChartComponent, {
      inputs: { symbol: 'SPY', liveBars: [], histBars: [] },
    });

    expect(
      screen.getByRole('button', { name: /expand live chart to fullscreen/i }),
    ).toBeTruthy();
    expect(
      screen.getByRole('button', { name: /expand history chart to fullscreen/i }),
    ).toBeTruthy();
  });

  it('renders live fill markers on the candle series', async () => {
    const markersFactory = vi.mocked(createSeriesMarkers);
    markersFactory.mockClear();

    await render(DualPaneChartComponent, {
      inputs: {
        symbol: 'SPY',
        liveBars: [
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
        liveFillMarkers: [
          {
            filled_at_ms: 1_700_000_030_000,
            side: 'buy',
            quantity: 2,
            price: 101,
            order_ref: 'order-1',
          },
        ],
        histBars: [],
      },
    });

    const livePlugin = markersFactory.mock.results[0]?.value;
    expect(livePlugin?.setMarkers).toHaveBeenCalledWith([
      expect.objectContaining({
        position: 'belowBar',
        shape: 'arrowUp',
        text: 'BUY 2',
      }),
    ]);
  });
});
