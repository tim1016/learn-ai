import type { Logical, LogicalRange } from 'lightweight-charts';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  candleDataForBar,
  candleSignatureForBars,
  filterActivityItemsForSymbol,
  isAtLiveEdge,
  isSnapshotTradeCoveredByActivity,
  localDateEndMs,
  localDateString,
  localDateStartMs,
  markerTimeForActivityFill,
  markerTimeForEventMs,
  visibleRangeToRestore,
} from './bot-trade-chart-card.component';
import type { ActivityFillMarker, IbkrMinuteBar } from './bot-trade-chart-card.types';

const range = (from: number, to: number): LogicalRange => ({
  from: from as Logical,
  to: to as Logical,
});

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
  source: 'ibkr',
});

const polygonBar = (startMs: number): IbkrMinuteBar => ({
  ...bar(startMs),
  source: 'polygon',
});

const mixedBar = (startMs: number): IbkrMinuteBar => ({
  ...bar(startMs),
  source: 'mixed',
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
  it('treats candle end as exclusive and assigns exact boundary timestamps to the next candle', () => {
    const bars = [bar(1_800_000), bar(1_860_000)];

    expect(markerTimeForEventMs(1_860_000, bars)).toBe(1_860);
  });

  it('snaps an intra-bar execution timestamp to its containing candle', () => {
    const bars = [bar(1_800_000), bar(1_860_000)];

    expect(markerTimeForEventMs(1_875_000, bars)).toBe(1_860);
  });

  it('snaps out-of-range event timestamps to the nearest displayed candle', () => {
    expect(markerTimeForEventMs(1_700_000, [bar(1_800_000)])).toBe(1_800);
    expect(markerTimeForEventMs(1_950_000, [bar(1_800_000), bar(1_860_000)])).toBe(1_860);
  });

  it('preserves the event timestamp when no candles are available', () => {
    expect(markerTimeForEventMs(1_700_000, [])).toBe(1_700);
  });
});

describe('visibleRangeToRestore', () => {
  it('does not restore a manual range while the chart is following live', () => {
    expect(visibleRangeToRestore(true, range(3, 8))).toBeNull();
  });

  it('restores the current range when the operator has panned away from live', () => {
    const current = range(3, 8);

    expect(visibleRangeToRestore(false, current)).toBe(current);
  });

  it('does not restore before lightweight-charts has emitted a range', () => {
    expect(visibleRangeToRestore(false, null)).toBeNull();
  });
});

describe('filterActivityItemsForSymbol', () => {
  it('keeps only markers and annotations for the chart symbol', () => {
    const items = [
      { id: 'tsla-open', symbol: 'TSLA' },
      { id: 'spy-open', symbol: 'SPY' },
      { id: 'tsla-close', symbol: 'tsla' },
    ];

    expect(filterActivityItemsForSymbol('tsla', items).map((item) => item.id)).toEqual([
      'tsla-open',
      'tsla-close',
    ]);
  });

  it('keeps all activity items when the activity symbol is blank', () => {
    const items = [{ id: 'tsla-open', symbol: 'TSLA' }];

    expect(filterActivityItemsForSymbol(' ', items)).toBe(items);
  });
});

describe('markerTimeForActivityFill', () => {
  it('uses the chart timestamp instead of the broker execution timestamp', () => {
    const bars = [bar(1_800_000), bar(1_860_000)];
    const marker: ActivityFillMarker = {
      id: 'exec-late',
      row_seq: 1,
      order_key: 'perm:1',
      symbol: 'SPY',
      side: 'BUY',
      quantity: 1,
      price: 100,
      chart_ts_ms: 1_875_000,
      exec_ts_ms: 8_000_000,
      position_effect: 'Open long',
      replay_count: 1,
      evidence: [],
    };

    expect(markerTimeForActivityFill(marker, bars)).toBe(1_860);
  });
});

describe('localDateString', () => {
  it('formats the operator-local calendar date without UTC conversion', () => {
    const localNoon = new Date(2026, 5, 24, 12, 0, 0);

    expect(localDateString(localNoon)).toBe('2026-06-24');
  });
});

describe('local date range bounds', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it('closes date ranges at the next local midnight across DST transitions', () => {
    vi.stubEnv('TZ', 'America/Chicago');

    expect((localDateEndMs('2026-03-08') - localDateStartMs('2026-03-08')) / 3_600_000)
      .toBe(23);
    expect((localDateEndMs('2026-11-01') - localDateStartMs('2026-11-01')) / 3_600_000)
      .toBe(25);
  });
});

describe('candleDataForBar', () => {
  it('uses default series styling for IBKR bars', () => {
    expect(candleDataForBar(bar(1_800_000))).toEqual({
      time: 1_800,
      open: 100,
      high: 101,
      low: 99,
      close: 100.5,
    });
  });

  it('styles Polygon overlays as muted historical candles', () => {
    expect(candleDataForBar(polygonBar(1_800_000))).toMatchObject({
      color: 'rgba(148, 163, 184, 0.24)',
      borderColor: 'rgba(203, 213, 225, 0.72)',
      wickColor: 'rgba(203, 213, 225, 0.58)',
    });
  });

  it('styles mixed IBKR/Polygon aggregate candles with a yellow border and wick', () => {
    expect(candleDataForBar(mixedBar(1_800_000))).toMatchObject({
      borderColor: '#facc15',
      wickColor: '#facc15',
    });
  });
});

describe('candleSignatureForBars', () => {
  it('changes when source styling inputs change without building a payload string', () => {
    expect(candleSignatureForBars([bar(1_800_000)]))
      .not.toBe(candleSignatureForBars([polygonBar(1_800_000)]));
  });
});

describe('isSnapshotTradeCoveredByActivity', () => {
  it('keeps snapshot trade markers for dates outside the activity projection session', () => {
    const activity = { session_date: '2026-06-29' };

    expect(isSnapshotTradeCoveredByActivity(new Date(2026, 5, 29, 10, 0).getTime(), activity))
      .toBe(true);
    expect(isSnapshotTradeCoveredByActivity(new Date(2026, 5, 28, 10, 0).getTime(), activity))
      .toBe(false);
  });
});
