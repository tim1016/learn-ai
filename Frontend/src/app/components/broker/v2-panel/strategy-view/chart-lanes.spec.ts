/** #2794 R4: the lanes under the strategy chart, from facts the page already has. */
import { describe, expect, it } from 'vitest';

import { formatTimestampDisplay } from '../../../../shared/timestamp/timestamp-display';
import {
  BEFORE_START_TEXT,
  STRATEGY_BAR_MS,
  STRATEGY_RUN_STARTED_AT_MS,
  STRATEGY_RUN_STOPPED_AT_MS,
  barCloseMs,
  fakeStrategyView,
} from '../../../../testing/strategy-view-fixtures';
import { chartLanes, formingBar, type ChartLane, type LaneKey, type StrategyRunContext } from './chart-lanes';

function minuteOf(ms: number): string {
  return formatTimestampDisplay(ms, { mode: 'local', granularity: 'minute' });
}

function context(overrides: Partial<StrategyRunContext> = {}): StrategyRunContext {
  return { nowMs: null, scheduledEndAtMs: null, fills: [], workingOrders: [], feedEvents: [], ...overrides };
}

function lane(lanes: readonly ChartLane[], key: LaneKey): ChartLane {
  const found = lanes.find((each) => each.key === key);
  if (found === undefined) throw new Error(`No ${key} lane.`);
  return found;
}

describe('chartLanes (#2794)', () => {
  it('draws four lanes in order: decisions, orders, market data, the bot', () => {
    expect(chartLanes(fakeStrategyView(), context()).map(({ title }) => title))
      .toEqual(['Decisions', 'Orders', 'Market data', 'Bot']);
  });

  it('marks every decision bar at its close, the before-start ones hollow in the backend’s words', () => {
    const marks = lane(chartLanes(fakeStrategyView(), context()), 'decisions').marks;

    expect(marks.map(({ atMs, glyph, label }) => ({ atMs, glyph, label }))).toEqual([
      { atMs: barCloseMs(0), glyph: 'ring', label: `${minuteOf(barCloseMs(0))} · ${BEFORE_START_TEXT}` },
      { atMs: barCloseMs(1), glyph: 'ring', label: `${minuteOf(barCloseMs(1))} · ${BEFORE_START_TEXT}` },
      { atMs: barCloseMs(2), glyph: 'dot', label: `${minuteOf(barCloseMs(2))} · Hold` },
      { atMs: barCloseMs(3), glyph: 'up', label: `${minuteOf(barCloseMs(3))} · Enter` },
    ]);
  });

  it('puts the run’s fills and working orders on the orders lane', () => {
    const marks = lane(chartLanes(fakeStrategyView(), context({
      fills: [{ filled_at_ms: barCloseMs(3) + 5_000, side: 'buy', quantity: 1, price: 501.25, order_ref: 'o-1', event_key: 'e-1' }],
      workingOrders: [{
        order_ref: 'o-2', broker_order_id: 'b-2', symbol: 'SPY', side: 'sell', quantity: 1, filled_quantity: 0,
        status: 'accepted', observed_at_ms: barCloseMs(3) + 60_000,
      }],
    })), 'orders').marks;

    expect(marks.map(({ glyph, tone, label }) => ({ glyph, tone, label }))).toEqual([
      { glyph: 'up', tone: 'bull', label: `${minuteOf(barCloseMs(3) + 5_000)} · Bought 1 @ 501.25` },
      { glyph: 'ring', tone: 'accent', label: `${minuteOf(barCloseMs(3) + 60_000)} · Working sell 1 SPY · Accepted` },
    ]);
  });

  it('spans a market-data gap across its window, once -- the joined minute’s included', () => {
    const joined = STRATEGY_RUN_STARTED_AT_MS - 5_000;
    const marks = lane(chartLanes(fakeStrategyView(), context({
      feedEvents: [{
        evidence_seq: 4, kind: 'gap', occurred_at_ms: joined + 60_000, label: 'Partial first minute omitted',
        explanation: 'x', cause: 'stream_joined', duration_ms: 55_000, duration_label: '55 s',
        window_start_ms: joined, window_end_ms: joined + 55_000,
      }],
    })), 'market').marks;

    expect(marks.map(({ atMs, endMs, glyph, tone, label }) => ({ atMs, endMs, glyph, tone, label }))).toEqual([{
      atMs: joined, endMs: joined + 55_000, glyph: 'span', tone: 'warn',
      label: `${minuteOf(joined + 60_000)} · Partial first minute omitted · 55 s`,
    }]);
  });

  it('marks a running bot’s start, next decision and an end still ahead', () => {
    const nowMs = barCloseMs(3) + 4 * 60_000;
    const endsAt = barCloseMs(3) + 3 * STRATEGY_BAR_MS;
    const marks = lane(chartLanes(fakeStrategyView(), context({ nowMs, scheduledEndAtMs: endsAt })), 'bot').marks;

    expect(marks.map(({ label }) => label)).toEqual([
      `Started ${minuteOf(STRATEGY_RUN_STARTED_AT_MS)}`,
      `Next decision ${minuteOf(barCloseMs(3) + STRATEGY_BAR_MS)}`,
      `Ends ${minuteOf(endsAt)}`,
    ]);
  });

  it('marks a stopped run’s end and no next decision', () => {
    const view = fakeStrategyView({ run_stopped_at_ms: STRATEGY_RUN_STOPPED_AT_MS });

    expect(lane(chartLanes(view, context({ scheduledEndAtMs: STRATEGY_RUN_STOPPED_AT_MS })), 'bot').marks
      .map(({ label }) => label))
      .toEqual([`Started ${minuteOf(STRATEGY_RUN_STARTED_AT_MS)}`, `Ended ${minuteOf(STRATEGY_RUN_STOPPED_AT_MS)}`]);
  });
});

describe('formingBar (#2794)', () => {
  it('is the bar after the last close while now falls inside it, and nothing otherwise', () => {
    const view = fakeStrategyView();
    const start = barCloseMs(3);

    expect(formingBar(view, start + 60_000)).toEqual({ startMs: start, closeMs: start + STRATEGY_BAR_MS });
    expect(formingBar(view, null)).toBeNull();
    // A read that has fallen behind the clock draws no forming bar it cannot place.
    expect(formingBar(view, start + STRATEGY_BAR_MS)).toBeNull();
  });
});
