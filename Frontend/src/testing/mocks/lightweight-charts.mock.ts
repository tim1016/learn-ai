import { vi } from 'vitest';

const mockTimeScale = {
  fitContent: vi.fn(),
};

const createMockSeries = () => ({
  setData: vi.fn(),
  applyOptions: vi.fn(),
});

const createMockChart = () => ({
  addSeries: vi.fn().mockReturnValue(createMockSeries()),
  removeSeries: vi.fn(),
  timeScale: vi.fn().mockReturnValue(mockTimeScale),
  applyOptions: vi.fn(),
  remove: vi.fn(),
});

export const createChart = vi.fn().mockImplementation(() => createMockChart());
export const CandlestickSeries = 'CandlestickSeries';
export const LineSeries = 'LineSeries';
export const HistogramSeries = 'HistogramSeries';

export type IChartApi = ReturnType<typeof createMockChart>;
export type ISeriesApi<T extends string = string> = ReturnType<typeof createMockSeries>;
export type CandlestickData = any;
export type LineData = any;
export type HistogramData = any;
export type UTCTimestamp = number;
