import type { UTCTimestamp } from 'lightweight-charts';

import type { ChartBar, ChartPositionPane } from '../lib/broker-v2-panel.types';

/** One unbroken stretch of the position pane's line. */
export interface PositionRun {
  readonly points: readonly { readonly time: UTCTimestamp; readonly value: number }[];
  /** The chart bars this stretch covers, for placing its fill markers. */
  readonly bars: readonly ChartBar[];
}

/**
 * The position pane's line as the stretches it is drawn in (#2915).
 *
 * The chart library joins every two points of one line, whatever lies
 * between them, so a bar with no price would be bridged from its neighbours.
 * Each stretch between missing bars is therefore its own line, and a missing
 * bar is a gap. `bars` are the chart's bars, which the pane's points follow
 * one for one.
 */
export function positionRuns(pane: ChartPositionPane, bars: readonly ChartBar[]): readonly PositionRun[] {
  const barAt = new Map(bars.map((bar) => [bar.start_ms, bar]));
  const runs: PositionRun[] = [];
  let points: { time: UTCTimestamp; value: number }[] = [];
  let covered: ChartBar[] = [];
  const close = (): void => {
    if (points.length > 0) runs.push({ points, bars: covered });
    points = [];
    covered = [];
  };
  for (const point of pane.points) {
    const bar = barAt.get(point.start_ms);
    if (point.value == null || bar === undefined) {
      close();
      continue;
    }
    points.push({ time: Math.floor(point.start_ms / 1000) as UTCTimestamp, value: Number(point.value) });
    covered.push(bar);
  }
  close();
  return runs;
}
