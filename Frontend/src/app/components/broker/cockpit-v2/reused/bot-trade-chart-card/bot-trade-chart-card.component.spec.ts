import type { Logical, LogicalRange } from 'lightweight-charts';
import { describe, expect, it } from 'vitest';
import {
  isAtLiveEdge,
  localDateString,
  markerTimeForEventMs,
} from './bot-trade-chart-card.component';
import type { IbkrMinuteBar } from './bot-trade-chart-card.types';

const range = (from: number, to: number): LogicalRange => ({
  from: from as Logical,
  to: to as Logical,
});

describe('isAtLiveEdge', () => {
  it('treats a null logical range (chart not yet rendered) as at-edge', () => {
    expect(isAtLiveEdge(null, 10)).toBe(true);
  });

  it('treats an empty bar set as at-edge — nothing to scroll back to', () => {
    expect(isAtLiveEdge(range(0, 0), 0)).toBe(true);
  });

  it('returns true when the visible range extends past the last bar', () => {
    // After a new bar lands, lightweight-charts floats range.to slightly past
    // the last logical index. Anything within half a bar of bars.length - 1
    // is still considered the live edge.
    expect(isAtLiveEdge(range(5, 9.2), 10)).toBe(true);
  });

  it('returns true when the visible range matches the last bar exactly', () => {
    expect(isAtLiveEdge(range(5, 9), 10)).toBe(true);
  });

  it('returns false when the user has scrolled back beyond the threshold', () => {
    // Visible range ends three bars before the latest — operator is panning
    // through history; the LIVE pill should dim.
    expect(isAtLiveEdge(range(0, 6), 10)).toBe(false);
  });

  it('honors a custom threshold for callers that need a stricter test', () => {
    // With a zero-bar threshold, range.to must be at or past the last index.
    expect(isAtLiveEdge(range(0, 8.6), 10, 0)).toBe(false);
    expect(isAtLiveEdge(range(0, 9.01), 10, 0)).toBe(true);
  });
});

describe('markerTimeForEventMs', () => {
  const bar = (startMs: number): IbkrMinuteBar => ({
    symbol: 'SPY',
    start_ms: startMs,
    end_ms: startMs + 60_000,
    open: '100',
    high: '101',
    low: '99',
    close: '100.5',
    volume: 100,
    fetched_at_ms: startMs + 60_000,
  });

  it('snaps a bar-close trade timestamp to the candle start time', () => {
    const bars = [bar(1_800_000), bar(1_860_000)];

    expect(markerTimeForEventMs(1_860_000, bars)).toBe(1_800);
  });

  it('snaps an intra-bar execution timestamp to its containing candle', () => {
    const bars = [bar(1_800_000), bar(1_860_000)];

    expect(markerTimeForEventMs(1_875_000, bars)).toBe(1_860);
  });

  it('preserves the original event timestamp when no candle contains it', () => {
    expect(markerTimeForEventMs(1_700_000, [bar(1_800_000)])).toBe(1_700);
  });
});

describe('localDateString', () => {
  it('formats the operator-local calendar date without UTC conversion', () => {
    const localNoon = new Date(2026, 5, 24, 12, 0, 0);

    expect(localDateString(localNoon)).toBe('2026-06-24');
  });
});
