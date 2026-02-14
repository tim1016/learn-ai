const mockTimeScale = {
  fitContent: jest.fn(),
};

const createMockSeries = () => ({
  setData: jest.fn(),
  applyOptions: jest.fn(),
});

const createMockChart = () => ({
  addSeries: jest.fn().mockReturnValue(createMockSeries()),
  removeSeries: jest.fn(),
  timeScale: jest.fn().mockReturnValue(mockTimeScale),
  applyOptions: jest.fn(),
  remove: jest.fn(),
});

export const createChart = jest.fn().mockImplementation(() => createMockChart());
export const CandlestickSeries = 'CandlestickSeries';
export const LineSeries = 'LineSeries';
export const HistogramSeries = 'HistogramSeries';

export type IChartApi = ReturnType<typeof createMockChart>;
export type ISeriesApi<T extends string = string> = ReturnType<typeof createMockSeries>;
export type CandlestickData = any;
export type LineData = any;
export type HistogramData = any;
export type UTCTimestamp = number;
