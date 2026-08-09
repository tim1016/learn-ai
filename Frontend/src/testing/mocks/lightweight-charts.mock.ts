import { vi } from 'vitest';

const mockTimeScale = {
  fitContent: vi.fn(),
};

const createMockSeries = () => ({
  setData: vi.fn(),
  applyOptions: vi.fn(),
  createPriceLine: vi.fn(),
});

const createMockChart = () => ({
  addSeries: vi.fn().mockReturnValue(createMockSeries()),
  removeSeries: vi.fn(),
  timeScale: vi.fn().mockReturnValue(mockTimeScale),
  panes: vi.fn(() => [{ setHeight: vi.fn() }]),
  applyOptions: vi.fn(),
  priceScale: vi.fn(() => ({ applyOptions: vi.fn() })),
  resize: vi.fn(),
  remove: vi.fn(),
});

export const createChart = vi.fn().mockImplementation(() => createMockChart());
export const CandlestickSeries = 'CandlestickSeries';
export const LineSeries = 'LineSeries';
export const HistogramSeries = 'HistogramSeries';
export const AreaSeries = 'AreaSeries';
export const LineType = { Simple: 0, WithSteps: 1 };
export const createSeriesMarkers = vi.fn();

export type IChartApi = ReturnType<typeof createMockChart>;
export type ISeriesApi<_T extends string = string> = ReturnType<typeof createMockSeries>;
export type CandlestickData = unknown;
export type LineData = unknown;
export type HistogramData = unknown;
export type UTCTimestamp = number;
