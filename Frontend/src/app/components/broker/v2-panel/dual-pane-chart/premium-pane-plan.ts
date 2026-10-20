import type { UTCTimestamp } from 'lightweight-charts';
import type { ChartBar, ChartPremiumPane, ChartRuleLine } from '../lib/broker-v2-panel.types';
import { positionRuns } from './dual-pane-chart-position';

const RULE_COLORS = { starting_price: '#9ca3af', momentum: '#eab308', stop: '#ef4444' };
const seconds = (ms: number): UTCTimestamp => Math.floor(ms / 1000) as UTCTimestamp;

export function ruleLinePlans(rules: readonly ChartRuleLine[], bars: readonly ChartBar[]) {
  return rules.map((rule) => {
    const covered = bars.filter((bar) => bar.end_ms >= rule.from_ms
      && (rule.to_ms == null || bar.start_ms < rule.to_ms));
    const endpoints = covered.length > 1 ? [covered[0], covered[covered.length - 1]] : covered;
    return {
      color: RULE_COLORS[rule.kind], title: `${rule.leg_id} ${rule.kind.replaceAll('_', ' ')}`,
      points: endpoints.map((bar) => ({ time: seconds(bar.start_ms), value: Number(rule.price) })),
    };
  });
}

/** Saved server prices, placed on the underlying's clock without bridging missing minutes. */
export function premiumPanePlans(panes: readonly ChartPremiumPane[], bars: readonly ChartBar[]) {
  return panes.map((pane) => ({
    symbol: pane.symbol,
    runs: positionRuns(pane, bars),
    rules: ruleLinePlans(pane.rule_lines, bars),
    fills: pane.fill_markers.flatMap((fill) => {
      const bar = bars.find((bar) => bar.start_ms <= fill.filled_at_ms && fill.filled_at_ms < bar.end_ms);
      return bar === undefined ? [] : [{
        time: seconds(bar.start_ms), value: fill.price,
        position: fill.side === 'buy' ? 'belowBar' as const : 'aboveBar' as const,
        shape: fill.side === 'buy' ? 'arrowUp' as const : 'arrowDown' as const,
        color: fill.side === 'buy' ? '#34d399' : '#fb7185',
        text: `${fill.symbol} · ${fill.exit_reason ?? fill.side} ${fill.quantity} @ ${fill.price}`,
      }];
    }).sort((a, b) => a.time - b.time),
  }));
}
