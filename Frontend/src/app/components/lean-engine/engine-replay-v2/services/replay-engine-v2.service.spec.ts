import { TestBed } from '@angular/core/testing';
import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import { ReplayEngineV2Service } from './replay-engine-v2.service';
import {
  StockAggregate, BacktestTrade, IndicatorSeries,
} from '../../../../graphql/types';

function makeBars(n: number, startMs = Date.UTC(2024, 0, 2, 14, 30)): StockAggregate[] {
  const bars: StockAggregate[] = [];
  for (let i = 0; i < n; i++) {
    const ts = startMs + i * 60_000;
    bars.push({
      id: i,
      timestamp: ts,
      open: 100 + i,
      high: 101 + i,
      low: 99 + i,
      close: 100.5 + i,
      volume: 1000,
      volumeWeightedAveragePrice: 100 + i,
      timespan: 'minute',
      multiplier: 1,
      transactionCount: 10,
    } as StockAggregate);
  }
  return bars;
}

function makeTrade(
  entryIdx: number, exitIdx: number, bars: StockAggregate[], pnl: number,
  type = 'long',
): BacktestTrade {
  return {
    tradeType: type,
    entryTimestamp: bars[entryIdx].timestamp,
    exitTimestamp: bars[exitIdx].timestamp,
    entryPrice: bars[entryIdx].close,
    exitPrice: bars[exitIdx].close,
    pnl,
    cumulativePnl: pnl,
    signalReason: 'test',
  };
}

function makeIndicator(name: string, window: number, bars: StockAggregate[]): IndicatorSeries {
  return {
    name,
    window,
    data: bars.map((b, i) => ({
      timestamp: b.timestamp,
      value: 50 + Math.sin(i / 5) * 10,
    })),
  } as IndicatorSeries;
}

describe('ReplayEngineV2Service', () => {
  let svc: ReplayEngineV2Service;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [ReplayEngineV2Service] });
    svc = TestBed.inject(ReplayEngineV2Service);
  });

  describe('load', () => {
    it('sorts bars ascending and numbers trades in entry order', () => {
      const bars = makeBars(10);
      const trades = [
        makeTrade(5, 7, bars, 5),
        makeTrade(1, 3, bars, -2),
      ];
      svc.load({ bars: [...bars].reverse(), trades, indicators: [] });
      expect(svc.totalBars()).toBe(10);
      expect(svc.currentIndex()).toBe(0);
      expect(svc.playbackState()).toBe('stopped');
      const numbered = svc.trades();
      expect(numbered.length).toBe(2);
      expect(numbered[0].tradeNumber).toBe(1);
      expect(numbered[0].entryMs).toBe(bars[1].timestamp);
      expect(numbered[1].tradeNumber).toBe(2);
    });
  });

  describe('playback', () => {
    it('stepForward advances and pauses at end', () => {
      const bars = makeBars(3);
      svc.load({ bars, trades: [], indicators: [] });
      svc.play();
      expect(svc.playbackState()).toBe('playing');
      svc.stepForward();
      svc.stepForward();
      expect(svc.currentIndex()).toBe(2);
      expect(svc.isAtEnd()).toBeTruthy();
      expect(svc.playbackState()).toBe('paused');
    });

    it('stepBackward does not go below 0', () => {
      const bars = makeBars(3);
      svc.load({ bars, trades: [], indicators: [] });
      svc.stepBackward();
      expect(svc.currentIndex()).toBe(0);
    });

    it('seekTo clamps to [0, total-1]', () => {
      const bars = makeBars(5);
      svc.load({ bars, trades: [], indicators: [] });
      svc.seekTo(99);
      expect(svc.currentIndex()).toBe(4);
      svc.seekTo(-5);
      expect(svc.currentIndex()).toBe(0);
    });

    it('seekToPercent maps proportionally', () => {
      const bars = makeBars(11);
      svc.load({ bars, trades: [], indicators: [] });
      svc.seekToPercent(0.5);
      expect(svc.currentIndex()).toBe(5);
    });

    it('toggleDirection flips forward/reverse', () => {
      svc.load({ bars: makeBars(5), trades: [], indicators: [] });
      expect(svc.direction()).toBe('forward');
      svc.toggleDirection();
      expect(svc.direction()).toBe('reverse');
    });

    describe('with fake timers', () => {
      beforeEach(() => vi.useFakeTimers());
      afterEach(() => vi.useRealTimers());

      it('reverse play steps backward on tick', () => {
        const bars = makeBars(10);
        svc.load({ bars, trades: [], indicators: [] });
        svc.seekTo(5);
        svc.setDirection('reverse');
        svc.setSpeed(10); // 100/10 = 10ms per tick
        svc.play();
        vi.advanceTimersByTime(25);
        expect(svc.currentIndex()).toBeLessThan(5);
        svc.pause();
      });
    });
  });

  describe('renderWindow', () => {
    it('returns all bars when windowSize = all', () => {
      const bars = makeBars(50);
      svc.load({ bars, trades: [], indicators: [] });
      svc.setWindowSize('all');
      svc.seekTo(25);
      const w = svc.renderWindow();
      expect(w.bars.length).toBe(50);
      expect(w.startIndex).toBe(0);
      expect(w.endIndex).toBe(49);
      expect(w.indexInWindow).toBe(25);
    });

    it('right-anchored when the cursor is far enough along', () => {
      const bars = makeBars(1000);
      svc.load({ bars, trades: [], indicators: [] });
      svc.setWindowSize(200);
      svc.seekTo(500);
      const w = svc.renderWindow();
      expect(w.bars.length).toBe(200);
      expect(w.endIndex).toBe(500);
      expect(w.startIndex).toBe(301);
      expect(w.indexInWindow).toBe(199);
    });

    it('grows from the left when the cursor is below windowSize', () => {
      const bars = makeBars(1000);
      svc.load({ bars, trades: [], indicators: [] });
      svc.setWindowSize(200);
      svc.seekTo(10);
      const w = svc.renderWindow();
      expect(w.startIndex).toBe(0);
      expect(w.endIndex).toBe(10);
      expect(w.bars.length).toBe(11);
      expect(w.indexInWindow).toBe(10);
    });

    it('clamps to available bars when total < windowSize', () => {
      const bars = makeBars(50);
      svc.load({ bars, trades: [], indicators: [] });
      svc.setWindowSize(200);
      svc.seekTo(25);
      const w = svc.renderWindow();
      expect(w.bars.length).toBe(26);
      expect(w.startIndex).toBe(0);
      expect(w.endIndex).toBe(25);
    });
  });

  describe('windowTrades and hiddenSummary', () => {
    it('classifies trades inside/left/right of window based on playhead', () => {
      const bars = makeBars(100);
      const trades = [
        makeTrade(5, 10, bars, 4),
        makeTrade(45, 55, bars, -3),
        makeTrade(80, 85, bars, 7),
      ];
      svc.load({ bars, trades, indicators: [] });
      svc.setWindowSize(40);
      svc.seekTo(50);

      const active = svc.activePosition();
      expect(active?.tradeNumber).toBe(2);

      const summary = svc.hiddenSummary();
      expect(summary.leftCount).toBe(1);
      expect(summary.leftCumPnl).toBeCloseTo(4);
      expect(summary.rightCount).toBe(0);
      expect(summary.rightCumPnl).toBe(0);
    });
  });

  describe('flashEvent', () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => vi.useRealTimers());

    it('fires exit kind when currentIndex crosses exit bar forward', () => {
      const bars = makeBars(10);
      const trades = [makeTrade(2, 5, bars, 3)];
      svc.load({ bars, trades, indicators: [] });
      svc.seekTo(4);
      svc.stepForward();
      const ev = svc.flashEvent();
      expect(ev?.kind).toBe('exit');
      expect(ev?.trade.pnl).toBe(3);
      vi.advanceTimersByTime(1500);
      expect(svc.flashEvent()).toBeNull();
    });

    it('fires unwind kind on reverse crossing of exit bar', () => {
      const bars = makeBars(10);
      const trades = [makeTrade(2, 5, bars, 3)];
      svc.load({ bars, trades, indicators: [] });
      svc.seekTo(5);
      svc.stepBackward();
      const ev = svc.flashEvent();
      expect(ev?.kind).toBe('unwind');
      vi.advanceTimersByTime(1500);
      expect(svc.flashEvent()).toBeNull();
    });

    it('seekTo clears any in-flight flash', () => {
      const bars = makeBars(10);
      const trades = [makeTrade(2, 5, bars, 3)];
      svc.load({ bars, trades, indicators: [] });
      svc.seekTo(4);
      svc.stepForward();
      expect(svc.flashEvent()).not.toBeNull();
      svc.seekTo(0);
      expect(svc.flashEvent()).toBeNull();
    });
  });

  describe('position', () => {
    it('reports flat between trades', () => {
      const bars = makeBars(20);
      const trades = [makeTrade(5, 8, bars, 2)];
      svc.load({ bars, trades, indicators: [] });
      svc.seekTo(10);
      const p = svc.position();
      expect(p.side).toBe('flat');
      expect(p.floatingPnl).toBeNull();
    });

    it('computes floating pnl for a long position', () => {
      const bars = makeBars(20);
      const trades = [makeTrade(5, 15, bars, 99, 'long')];
      svc.load({ bars, trades, indicators: [] });
      svc.seekTo(10);
      const p = svc.position();
      expect(p.side).toBe('long');
      expect(p.entryPrice).toBeCloseTo(bars[5].close);
      expect(p.floatingPnl).toBeCloseTo(bars[10].close - bars[5].close);
      expect(p.barsHeld).toBe(6);
    });

    it('inverts floating pnl for a short position', () => {
      const bars = makeBars(20);
      const trades = [makeTrade(5, 15, bars, 99, 'short')];
      svc.load({ bars, trades, indicators: [] });
      svc.seekTo(10);
      const p = svc.position();
      expect(p.side).toBe('short');
      expect(p.floatingPnl).toBeCloseTo(bars[5].close - bars[10].close);
    });
  });

  describe('signalCards', () => {
    it('emits currentValue, entryValue, and delta during an open position', () => {
      const bars = makeBars(30);
      const indicators = [makeIndicator('rsi', 14, bars)];
      const trades = [makeTrade(5, 25, bars, 1)];
      svc.load({ bars, trades, indicators });
      svc.seekTo(12);
      const cards = svc.signalCards();
      expect(cards.length).toBe(1);
      expect(cards[0].currentValue).not.toBeNull();
      expect(cards[0].entryValue).not.toBeNull();
      expect(cards[0].delta).toBeCloseTo(
        (cards[0].currentValue ?? 0) - (cards[0].entryValue ?? 0),
      );
    });

    it('entryValue is null when flat', () => {
      const bars = makeBars(30);
      const indicators = [makeIndicator('rsi', 14, bars)];
      svc.load({ bars, trades: [], indicators });
      svc.seekTo(10);
      const cards = svc.signalCards();
      expect(cards[0].entryValue).toBeNull();
      expect(cards[0].delta).toBeNull();
    });
  });

  describe('reset', () => {
    it('clears everything', () => {
      const bars = makeBars(10);
      svc.load({ bars, trades: [], indicators: [] });
      svc.seekTo(5);
      svc.setSpeed(4);
      svc.setDirection('reverse');
      svc.reset();
      expect(svc.totalBars()).toBe(0);
      expect(svc.currentIndex()).toBe(0);
      expect(svc.direction()).toBe('forward');
      expect(svc.playbackSpeed()).toBe(1);
      expect(svc.flashEvent()).toBeNull();
    });
  });
});
