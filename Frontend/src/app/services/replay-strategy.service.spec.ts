import { TestBed } from '@angular/core/testing';
import { ReplayStrategyService } from './replay-strategy.service';
import { ReplayEngineService } from './replay-engine.service';
import { BacktestTrade } from '../graphql/types';
import { createMockAggregatesTimeSeries } from '../../testing/factories/market-data.factory';

function createMockTrade(overrides: Partial<BacktestTrade> = {}): BacktestTrade {
  return {
    tradeType: 'LONG',
    entryTimestamp: '2026-01-05T09:35:00.000Z',
    exitTimestamp: '2026-01-05T09:40:00.000Z',
    entryPrice: 150,
    exitPrice: 152,
    pnl: 2,
    cumulativePnl: 2,
    signalReason: 'SMA crossover',
    ...overrides,
  };
}

describe('ReplayStrategyService', () => {
  let service: ReplayStrategyService;
  let replayEngine: ReplayEngineService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
    service = TestBed.inject(ReplayStrategyService);
    replayEngine = TestBed.inject(ReplayEngineService);
  });

  afterEach(() => {
    service.reset();
    replayEngine.reset();
  });

  it('should create', () => {
    expect(service).toBeTruthy();
  });

  it('should return empty when no trades loaded', () => {
    const bars = createMockAggregatesTimeSeries(5, 1);
    replayEngine.load(bars);
    expect(service.visibleTrades()).toEqual([]);
    expect(service.completedTrades()).toEqual([]);
    expect(service.activePosition()).toBeNull();
  });

  it('should return empty when no data loaded in replay engine', () => {
    service.loadTrades([createMockTrade()]);
    expect(service.visibleTrades()).toEqual([]);
    expect(service.activePosition()).toBeNull();
  });

  it('should show trade only when entryTimestamp <= current bar timestamp', () => {
    // 20 bars, 1-minute intervals starting at 09:30
    const bars = createMockAggregatesTimeSeries(20, 1);
    replayEngine.load(bars);

    // Trade enters at bar index 5 (09:35) and exits at bar index 10 (09:40)
    const trade = createMockTrade({
      entryTimestamp: bars[5].timestamp,
      exitTimestamp: bars[10].timestamp,
    });
    service.loadTrades([trade]);

    // Before entry: trade not visible
    replayEngine.seekTo(4);
    expect(service.visibleTrades().length).toBe(0);

    // At entry: trade becomes visible
    replayEngine.seekTo(5);
    expect(service.visibleTrades().length).toBe(1);

    // After entry: still visible
    replayEngine.seekTo(7);
    expect(service.visibleTrades().length).toBe(1);
  });

  it('should detect active position (entered but not yet exited)', () => {
    const bars = createMockAggregatesTimeSeries(20, 1);
    replayEngine.load(bars);

    const trade = createMockTrade({
      entryTimestamp: bars[5].timestamp,
      exitTimestamp: bars[10].timestamp,
    });
    service.loadTrades([trade]);

    // Before entry: no active position
    replayEngine.seekTo(4);
    expect(service.activePosition()).toBeNull();

    // During position: active
    replayEngine.seekTo(7);
    expect(service.activePosition()).not.toBeNull();
    expect(service.activePosition()!.entryTimestamp).toBe(bars[5].timestamp);

    // At exit: no longer active (exit already happened)
    replayEngine.seekTo(10);
    expect(service.activePosition()).toBeNull();

    // After exit: not active
    replayEngine.seekTo(15);
    expect(service.activePosition()).toBeNull();
  });

  it('should distinguish completed trades from active positions', () => {
    const bars = createMockAggregatesTimeSeries(20, 1);
    replayEngine.load(bars);

    const trade = createMockTrade({
      entryTimestamp: bars[3].timestamp,
      exitTimestamp: bars[8].timestamp,
    });
    service.loadTrades([trade]);

    // During position: visible but not completed
    replayEngine.seekTo(5);
    expect(service.visibleTrades().length).toBe(1);
    expect(service.completedTrades().length).toBe(0);
    expect(service.activePosition()).not.toBeNull();

    // After exit: visible and completed
    replayEngine.seekTo(8);
    expect(service.visibleTrades().length).toBe(1);
    expect(service.completedTrades().length).toBe(1);
    expect(service.activePosition()).toBeNull();
  });

  it('should enforce no-lookahead: no future trades visible at any replay point', () => {
    const bars = createMockAggregatesTimeSeries(20, 1);
    replayEngine.load(bars);

    const trades = [
      createMockTrade({
        entryTimestamp: bars[3].timestamp,
        exitTimestamp: bars[6].timestamp,
        pnl: 2,
        cumulativePnl: 2,
      }),
      createMockTrade({
        entryTimestamp: bars[10].timestamp,
        exitTimestamp: bars[15].timestamp,
        pnl: -1,
        cumulativePnl: 1,
      }),
    ];
    service.loadTrades(trades);

    for (let n = 0; n < 20; n++) {
      replayEngine.seekTo(n);
      const currentTimestampMs = new Date(replayEngine.currentBar()!.timestamp).getTime();

      for (const trade of service.visibleTrades()) {
        expect(new Date(trade.entryTimestamp).getTime()).toBeLessThanOrEqual(currentTimestampMs);
      }

      for (const trade of service.completedTrades()) {
        expect(new Date(trade.exitTimestamp).getTime()).toBeLessThanOrEqual(currentTimestampMs);
      }

      const active = service.activePosition();
      if (active) {
        expect(new Date(active.entryTimestamp).getTime()).toBeLessThanOrEqual(currentTimestampMs);
        expect(new Date(active.exitTimestamp).getTime()).toBeGreaterThan(currentTimestampMs);
      }
    }
  });

  it('should handle multiple trades sequentially', () => {
    const bars = createMockAggregatesTimeSeries(20, 1);
    replayEngine.load(bars);

    const trades = [
      createMockTrade({
        entryTimestamp: bars[2].timestamp,
        exitTimestamp: bars[5].timestamp,
        pnl: 3,
        cumulativePnl: 3,
      }),
      createMockTrade({
        entryTimestamp: bars[8].timestamp,
        exitTimestamp: bars[12].timestamp,
        pnl: -1,
        cumulativePnl: 2,
      }),
      createMockTrade({
        entryTimestamp: bars[14].timestamp,
        exitTimestamp: bars[18].timestamp,
        pnl: 5,
        cumulativePnl: 7,
      }),
    ];
    service.loadTrades(trades);

    // Before any trade
    replayEngine.seekTo(1);
    expect(service.visibleTrades().length).toBe(0);
    expect(service.completedTrades().length).toBe(0);

    // During first trade
    replayEngine.seekTo(3);
    expect(service.visibleTrades().length).toBe(1);
    expect(service.completedTrades().length).toBe(0);

    // Between trades 1 and 2
    replayEngine.seekTo(6);
    expect(service.visibleTrades().length).toBe(1);
    expect(service.completedTrades().length).toBe(1);

    // During second trade
    replayEngine.seekTo(9);
    expect(service.visibleTrades().length).toBe(2);
    expect(service.completedTrades().length).toBe(1);

    // All trades completed
    replayEngine.seekTo(19);
    expect(service.visibleTrades().length).toBe(3);
    expect(service.completedTrades().length).toBe(3);
    expect(service.activePosition()).toBeNull();
  });

  it('should reset trade data', () => {
    service.loadTrades([createMockTrade()]);
    expect(service.allTrades().length).toBe(1);

    service.reset();
    expect(service.allTrades().length).toBe(0);
  });
});
