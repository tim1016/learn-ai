import { TestBed } from '@angular/core/testing';
import { ReplayIndicatorService } from './replay-indicator.service';
import { ReplayEngineService } from './replay-engine.service';
import { IndicatorSeries } from '../graphql/types';
import { createMockAggregatesTimeSeries } from '../../testing/factories/market-data.factory';

function createIndicatorSeries(
  name: string,
  window: number,
  timestamps: number[],
): IndicatorSeries {
  return {
    name,
    window,
    data: timestamps.map((ts, i) => ({
      timestamp: ts,
      value: 150 + i * 0.5,
      signal: null,
      histogram: null,
      upper: null,
      lower: null,
    })),
  };
}

describe('ReplayIndicatorService', () => {
  let service: ReplayIndicatorService;
  let replayEngine: ReplayEngineService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
    service = TestBed.inject(ReplayIndicatorService);
    replayEngine = TestBed.inject(ReplayEngineService);
  });

  afterEach(() => {
    service.reset();
    replayEngine.reset();
  });

  it('should create', () => {
    expect(service).toBeTruthy();
  });

  it('should return empty when no indicators loaded', () => {
    const bars = createMockAggregatesTimeSeries(5, 1);
    replayEngine.load(bars);
    expect(service.visibleIndicators()).toEqual([]);
  });

  it('should return empty when no data loaded in replay engine', () => {
    const timestamps = [
      new Date('2026-01-05T09:30:00Z').getTime(),
      new Date('2026-01-05T09:31:00Z').getTime(),
    ];
    service.loadIndicators([createIndicatorSeries('sma', 14, timestamps)]);
    expect(service.visibleIndicators()).toEqual([]);
  });

  it('should enforce no-lookahead: at index N, no indicator point has timestamp > bars[N].timestamp', () => {
    const bars = createMockAggregatesTimeSeries(10, 1);
    replayEngine.load(bars);

    // Create indicator points that match bar timestamps exactly
    const timestamps = bars.map(b => new Date(b.timestamp).getTime());
    service.loadIndicators([createIndicatorSeries('sma', 14, timestamps)]);

    for (let n = 0; n < 10; n++) {
      replayEngine.seekTo(n);
      const currentTimestampMs = new Date(replayEngine.currentBar()!.timestamp).getTime();
      const visible = service.visibleIndicators();

      for (const series of visible) {
        for (const point of series.data) {
          expect(point.timestamp).toBeLessThanOrEqual(currentTimestampMs);
        }
      }
    }
  });

  it('should progressively reveal more indicator points as replay advances', () => {
    const bars = createMockAggregatesTimeSeries(10, 1);
    replayEngine.load(bars);

    const timestamps = bars.map(b => new Date(b.timestamp).getTime());
    service.loadIndicators([createIndicatorSeries('sma', 14, timestamps)]);

    const counts: number[] = [];
    for (let n = 0; n < 10; n++) {
      replayEngine.seekTo(n);
      counts.push(service.visibleIndicators()[0].data.length);
    }

    // Each step should have >= previous count
    for (let i = 1; i < counts.length; i++) {
      expect(counts[i]).toBeGreaterThanOrEqual(counts[i - 1]);
    }

    // At the end, all points should be visible
    expect(counts[9]).toBe(10);
  });

  it('should include indicator point at exact same timestamp as current bar', () => {
    const bars = createMockAggregatesTimeSeries(5, 1);
    replayEngine.load(bars);

    const exactTimestamp = new Date(bars[2].timestamp).getTime();
    service.loadIndicators([createIndicatorSeries('sma', 14, [exactTimestamp])]);

    replayEngine.seekTo(2);
    const visible = service.visibleIndicators();
    expect(visible[0].data.length).toBe(1);
    expect(visible[0].data[0].timestamp).toBe(exactTimestamp);
  });

  it('should handle multiple indicator series independently', () => {
    const bars = createMockAggregatesTimeSeries(5, 1);
    replayEngine.load(bars);

    const ts0 = new Date(bars[0].timestamp).getTime();
    const ts2 = new Date(bars[2].timestamp).getTime();
    const ts4 = new Date(bars[4].timestamp).getTime();

    service.loadIndicators([
      createIndicatorSeries('sma', 14, [ts0, ts2, ts4]),
      createIndicatorSeries('ema', 14, [ts0, ts4]),
    ]);

    replayEngine.seekTo(2);
    const visible = service.visibleIndicators();
    expect(visible.length).toBe(2);
    expect(visible[0].data.length).toBe(2); // ts0 and ts2
    expect(visible[1].data.length).toBe(1); // only ts0
  });

  it('should reset indicator data', () => {
    service.loadIndicators([createIndicatorSeries('sma', 14, [1000])]);
    expect(service.indicatorSeries().length).toBe(1);

    service.reset();
    expect(service.indicatorSeries().length).toBe(0);
  });
});
