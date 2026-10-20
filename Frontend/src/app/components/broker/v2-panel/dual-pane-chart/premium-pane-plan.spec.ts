import { expect, it } from 'vitest';
import type { ChartBar, ChartPremiumPane } from '../lib/broker-v2-panel.types';
import { premiumPanePlans } from './premium-pane-plan';

const FIRST = 1_791_552_600_000;
const bars: ChartBar[] = [0, 1, 2, 3].map((minute) => ({
  start_ms: FIRST + minute * 60_000, end_ms: FIRST + (minute + 1) * 60_000,
  open: '500', high: '500', low: '500', close: '500', volume: 1, source: 'ibkr',
}));

export const premium: ChartPremiumPane = {
  symbol: 'SPY261009C00500000',
  contract: { root: 'SPY', expiry_ms: FIRST + 23_400_000, right: 'call', strike_thousandths: 500000 },
  multiplier: 100, leg_ids: ['call'], entry_numbers: [0],
  points: bars.map((bar, index) => ({ start_ms: bar.start_ms, value: index === 1 ? null : '2' })),
  rule_lines: [{ kind: 'momentum', price: '2.5', from_ms: bars[0].end_ms, to_ms: bars[2].end_ms, leg_id: 'call', entry_number: 0 }],
  fill_markers: [{ filled_at_ms: bars[3].start_ms, side: 'sell', quantity: 1, price: 1.4,
    symbol: 'SPY261009C00500000', order_ref: 'exit', event_key: 'fill', exit_reason: 'Stop' }],
};

it('keeps pick order, missing-minute gaps, finite levels and contract exit reasons on the shared clock', () => {
  const plans = premiumPanePlans([premium, { ...premium, symbol: 'second' }], bars);
  expect(plans.map((plan) => plan.symbol)).toEqual([premium.symbol, 'second']);
  expect(plans[0].runs.map((run) => run.points.length)).toEqual([1, 2]);
  expect(plans[0].rules[0].points.map((point) => point.time)).toEqual([bars[1].start_ms / 1000, bars[2].start_ms / 1000]);
  expect(plans[0].fills[0].text).toContain('SPY261009C00500000 · Stop');
  expect(plans[0].fills[0].time).toBe(bars[3].start_ms / 1000);
});

it('distinguishes backtest rules, fills and backtest-only contract series', () => {
  const plan = premiumPanePlans([{ ...premium, backtest_only: true,
    points: [], backtest_points: premium.points,
    rule_lines: premium.rule_lines.map(rule => ({ ...rule, source: 'backtest' })),
    fill_markers: premium.fill_markers.map(fill => ({ ...fill, source: 'backtest' })),
  }], bars)[0];
  expect(plan.runs).toEqual([]);
  expect(plan.backtestRuns.map(run => run.points.length)).toEqual([1, 2]);
  expect(plan.rules[0].backtest).toBe(true);
  expect(plan.rules[0].title).toContain('Backtest');
  expect(plan.fills[0].text).toContain('Backtest ·');
  expect(plan.fills[0].color).toContain('rgba');
});


it('keeps different live and modeled prices when fills share the same minute', () => {
  const live = premium.fill_markers[0];
  const plan = premiumPanePlans([{ ...premium, fill_markers: [live,
    { ...live, source: 'backtest', price: 2.1, event_key: 'modeled-fill' },
  ] }], bars)[0];
  expect(plan.fillBounds).toEqual([
    [{ time: bars[3].start_ms / 1000, value: 1.4 }],
    [{ time: bars[3].start_ms / 1000, value: 2.1 }],
  ]);
  expect(plan.fills.map(fill => ({ time: fill.time, position: fill.position, price: fill.price })))
    .toEqual([
      { time: bars[3].start_ms / 1000, position: 'atPriceMiddle', price: 1.4 },
      { time: bars[3].start_ms / 1000, position: 'atPriceMiddle', price: 2.1 },
    ]);
});
