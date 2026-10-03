import type { ChartAction, ChartOption } from './golden-search-echarts';
import type { ChartTheme } from './golden-search-chart-theme';

/** The chart's values as a table: the keyboard and screen-reader route to every value. `cells[0]` heads its row. */
export interface ChartTable {
  readonly caption: string;
  readonly columns: readonly string[];
  readonly rows: readonly { readonly key: string; readonly cells: readonly string[] }[];
}

/**
 * Everything the chart host draws for one chart. Series ids are
 * `<group>:<key>` (`equity:all_period`), so a walkthrough step can point at a
 * group and the host can find the featured candidate's series in it.
 */
export interface ChartSpec {
  /** Names the chart, e.g. "Development cumulative return and fall from peak". */
  readonly label: string;
  /** One sentence saying what the chart shows, for assistive technology. */
  readonly summary: string;
  /** The key of the series a walkthrough points at first (the selected candidate). */
  readonly featured: string | null;
  readonly option: (theme: ChartTheme) => ChartOption;
  readonly table: ChartTable;
}

/**
 * Whether two specs draw the same chart: the same name, summary, featured
 * series and table. A spec's option and table come from the same server
 * values, so a study poll that changes none of them need not redraw.
 */
export function sameChart(a: ChartSpec | null, b: ChartSpec | null): boolean {
  if (a === null || b === null) return a === b;
  const [x, y] = [a.table, b.table];
  return (
    a.label === b.label &&
    a.summary === b.summary &&
    a.featured === b.featured &&
    x.caption === y.caption &&
    sameStrings(x.columns, y.columns) &&
    x.rows.length === y.rows.length &&
    x.rows.every((row, i) => row.key === y.rows[i].key && sameStrings(row.cells, y.rows[i].cells))
  );
}

function sameStrings(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((text, i) => text === b[i]);
}

/** What a walkthrough step lights up: every series in a group, or one point of the featured series. */
export type HighlightTarget =
  | { readonly kind: 'series'; readonly group: string }
  | { readonly kind: 'point'; readonly group: string; readonly at: 'last' | 'lowest' };

interface SeriesRef {
  readonly index: number;
  readonly id: string;
  readonly values: readonly (number | null)[];
}

/**
 * The ECharts actions that show `target` on a drawn option: clear the last
 * highlight, then emphasise the group (the other series dim) and, for a
 * point, open its tooltip. A null target only clears. Finding the lowest
 * point positions the highlight; the tooltip shows the server's values there.
 */
export function highlightActions(option: ChartOption, target: HighlightTarget | null, featured: string | null): ChartAction[] {
  // hideTip leaves a shown point's axis pointer drawn; a pointer 'leave' clears it.
  const clear: ChartAction[] = [{ type: 'downplay' }, { type: 'hideTip' }, { type: 'updateAxisPointer', currTrigger: 'leave' }];
  if (target === null) return clear;
  const group = seriesRefs(option).filter((series) => series.id.startsWith(`${target.group}:`));
  if (group.length === 0) return clear;
  const highlight: ChartAction = { type: 'highlight', seriesIndex: group.map((series) => series.index) };
  if (target.kind === 'series') return [...clear, highlight];
  const series = group.find((candidate) => candidate.id === `${target.group}:${featured}`) ?? group[0];
  const dataIndex = target.at === 'last' ? lastIndex(series.values) : lowestIndex(series.values);
  // The tooltip first: its axis pointer emphasises every line at that session, which the group highlight then overrides.
  return dataIndex === null ? [...clear, highlight] : [...clear, { type: 'showTip', seriesIndex: series.index, dataIndex }, highlight];
}

function seriesRefs(option: ChartOption): SeriesRef[] {
  const series = option.series === undefined ? [] : Array.isArray(option.series) ? option.series : [option.series];
  return series.map((entry, index) => ({ index, id: String(entry.id ?? ''), values: Array.isArray(entry.data) ? entry.data.map(numberOrNull) : [] }));
}

/** A plotted value — a number, or a data pair's last coordinate — or null for ECharts' empty point (`'-'`). */
function numberOrNull(value: unknown): number | null {
  const plotted: unknown = Array.isArray(value) ? value.at(-1) : value;
  return typeof plotted === 'number' && Number.isFinite(plotted) ? plotted : null;
}

function lastIndex(values: readonly (number | null)[]): number | null {
  for (let i = values.length - 1; i >= 0; i--) if (values[i] !== null) return i;
  return null;
}

function lowestIndex(values: readonly (number | null)[]): number | null {
  let found: number | null = null;
  let lowest = Infinity;
  for (let i = 0; i < values.length; i++) {
    const value = values[i];
    if (value !== null && value < lowest) {
      lowest = value;
      found = i;
    }
  }
  return found;
}
