import { describe, expect, it } from 'vitest';

import type { ChartBar, ChartPositionPane } from '../lib/broker-v2-panel.types';
import { positionRuns } from './dual-pane-chart-position';

const MINUTE = 60_000;
const FIRST = 1_700_060_400_000;

function bar(index: number): ChartBar {
  const start = FIRST + index * MINUTE;
  return { start_ms: start, end_ms: start + MINUTE, open: '450', high: '450', low: '450', close: '450', volume: 1, source: 'polygon' };
}

function pane(values: readonly (string | null)[]): ChartPositionPane {
  return {
    kind: 'price',
    symbol: 'QQQ',
    heading: 'What it trades',
    points: values.map((value, index) => ({ start_ms: FIRST + index * MINUTE, value })),
    fill_markers: [],
    overlay_notices: [],
  };
}

describe('positionRuns (#2915)', () => {
  it('breaks the line at a bar with no price instead of joining its neighbours', () => {
    const bars = [bar(0), bar(1), bar(2), bar(3)];

    const runs = positionRuns(pane(['512.40', null, '512.90', '513.00']), bars);

    expect(runs.map((run) => run.points.map((point) => point.value))).toEqual([[512.4], [512.9, 513]]);
    expect(runs.map((run) => run.bars)).toEqual([[bars[0]], [bars[2], bars[3]]]);
  });

  it('draws nothing when the traded asset has no price in the window', () => {
    expect(positionRuns(pane([null, null]), [bar(0), bar(1)])).toEqual([]);
  });
});
