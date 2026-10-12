import { optionContractName } from '../../../shared/option-contract/option-contract.component';
import { formatAuthoredUsd } from '../../../shared/pipes/authored-usd.pipe';
import { formatReceiptLabel } from '../../../shared/pipes/receipt-label.pipe';
import type { ChartSeriesColorToken } from '../../../shared/trading-chart/chart-series-color-tokens';
import { axisPrice } from './series-geometry';
import type { SeriesPoint, SeriesReason, SessionDetail, SessionFill, SessionLevel, SessionMark, SessionSeries, TradeContract } from './run-detail.types';
import { contractOf, FILL_WORDS, knownContracts, legAttempt, LEVEL_WORDS, MARK_WORDS } from './words';

/**
 * What the one-day chart is handed, worked out from the server's session answer and nothing else. A pure function: the
 * chart wrapper draws exactly this, and a spec reads exactly this.
 *
 * Rules it keeps:
 * - A minute with no trade has no point, and the line breaks there. The library joins a line across a gap in its time
 *   (a point of no value only holds a slot on the time scale), so a break is made by ending one line series and starting
 *   another: a contract's minute closes go out as one line series for each unbroken run of minutes. Nothing is carried
 *   forward, smoothed or filled.
 * - A series of no values at all holds one slot for every minute of the session (the "clock"), so a gap keeps its width
 *   and every pane keeps the same time axis.
 * - A series the server withholds is not given to the chart. Its levels, fills and marks are not drawn on an empty pane.
 * - A level is drawn only between the two instants the server gave it.
 * - Colours are token ids; the wrapper resolves them to colours at the library's boundary.
 * - Every price a person reads from the chart (a tip) is the server's text, put through the shared formatter, found by
 *   the instant. The only numbers here are positions, from `axisPrice`, in `value` and `price` fields that are never printed.
 */

/** A bar is a minute: a point is the close of one, and two points one bar apart are in one unbroken run. */
export const BAR_MS = 60_000;
/** A session is never longer than a day; a grid longer than that is a server error, and no clock is made for it. */
const MAX_GRID_MINUTES = 24 * 60;

export type PlanLineStyle = 'solid' | 'dotted' | 'dashed' | 'large-dashed';
export type SeriesRole = 'clock' | 'closes' | 'level' | 'fills';
export type MarkerShape = 'arrowUp' | 'arrowDown' | 'square';

/** A point on the chart: seconds since the epoch, and a position on the price axis. */
export interface PlanPoint {
  readonly time: number;
  readonly value: number;
}

/** A slot of the time scale with no value in it. */
export interface PlanSlot {
  readonly time: number;
}

export interface PlanMarker {
  readonly id: string;
  readonly time: number;
  /** Where on the price axis the marker sits: the price filled, or the price a leg was marked at. */
  readonly price: number;
  readonly shape: MarkerShape;
  readonly token: ChartSeriesColorToken;
  readonly text: string;
}

export interface SeriesPlan {
  readonly id: string;
  readonly role: SeriesRole;
  /** The pane it is drawn on, from 0 at the top. */
  readonly pane: number;
  readonly token: ChartSeriesColorToken;
  readonly lineStyle: PlanLineStyle;
  readonly lineWidth: 1 | 2;
  /** False for a series that only holds markers. */
  readonly lineVisible: boolean;
  /** True for a line of one point: a lone minute is a stroke a few pixels long, so the point is drawn as a dot. */
  readonly pointMarkers: boolean;
  readonly data: readonly (PlanPoint | PlanSlot)[];
  readonly markers: readonly PlanMarker[];
}

/** One line of a pane's legend: what a line or mark is, in words, and how it is drawn so colour is never the only sign. */
export interface LegendEntry {
  readonly key: string;
  readonly name: string;
  readonly drawn: string;
  readonly token: ChartSeriesColorToken;
  readonly sample: 'line' | 'marker';
  readonly lineStyle: PlanLineStyle;
  readonly glyph: string;
}

export interface PanePlan {
  readonly key: string;
  /** The underlying's ticker, or the contract in words. */
  readonly caption: string;
  /** The ticker when the pane is the underlying's; the contract's parsed fields when it is a contract's. */
  readonly symbol: string;
  readonly contract: TradeContract | null;
  readonly entries: readonly LegendEntry[];
}

/** Something the chart does not draw, and why: the server's words for a series it withheld, or the page's own sentence. */
export interface NotDrawn {
  readonly key: string;
  /** What was not drawn when it is a level, a fill or a mark ("Stop level"); null when it is a whole series. */
  readonly what: string | null;
  /** The instrument it is of: the underlying's ticker, or a contract's OCC symbol. */
  readonly symbol: string;
  /** True when the instrument is the underlying, whose ticker the shared symbol renderer names. */
  readonly ofUnderlying: boolean;
  /** The contract's parsed fields, for the shared renderer; null for the underlying and when the server sent none. */
  readonly contract: TradeContract | null;
  /** The server's reason; null when the page itself could not place the series. */
  readonly reason: SeriesReason | null;
  readonly message: string;
  /** True when levels, fills or marks of this contract are listed as text though they are not drawn. */
  readonly listedBelow: boolean;
}

export interface TimelinePlan {
  readonly panes: readonly PanePlan[];
  readonly series: readonly SeriesPlan[];
  readonly notDrawn: readonly NotDrawn[];
  /** For each minute close (seconds), the lines the tooltip shows: the server's own price strings, found by that instant. */
  readonly tips: ReadonlyMap<number, readonly string[]>;
  /** The chart's name for those who cannot see it. */
  readonly label: string;
}

/** The page could not place a series on a chart: a minute out of order, or a price that is not a plain decimal. */
export const UNPLACEABLE = 'The page cannot place this series on a chart (a minute is out of order or a price is not a number), so it is not drawn.';

/** The server withheld a series and sent neither a message nor a reason that says why. */
export const NO_REASON_GIVEN = 'The server did not say why this series is withheld, so it is not drawn.';

/** The server called a series shown and sent no minute of it. */
export const NO_MINUTES = 'The server sent no minute closes for this series, so there is nothing to draw.';

const TOKENS = {
  underlying: 'series-blue',
  contract: 'series-sky',
  entry: 'series-green',
  exit: 'series-orange',
  mark: 'series-purple',
} as const satisfies Record<string, ChartSeriesColorToken>;

const LEVEL_DRAWING: Readonly<Record<SessionLevel['kind'], { readonly token: ChartSeriesColorToken; readonly style: PlanLineStyle }>> = {
  starting_price: { token: 'series-amber', style: 'dotted' },
  momentum: { token: 'series-violet', style: 'dashed' },
  stop: { token: 'series-red', style: 'large-dashed' },
};

const seconds = (ms: number): number => ms / 1000;

/** Every minute close of a session, in seconds: the minute after its open to its close. Empty if the session is longer than a day. */
function gridOf(openMs: number, closeMs: number): number[] {
  const minutes = (closeMs - openMs) / BAR_MS;
  if (!(minutes > 0) || minutes > MAX_GRID_MINUTES) return [];
  const grid: number[] = [];
  for (let at = openMs + BAR_MS; at <= closeMs; at += BAR_MS) grid.push(seconds(at));
  return grid;
}

/** A point of a series with its position on the price axis. */
interface Placed {
  readonly point: SeriesPoint;
  readonly value: number;
}

/**
 * The unbroken runs of a series' points: a new run starts wherever a minute has no point. Null when the points cannot be
 * placed (not in strictly rising order, or a price that is not a plain decimal).
 */
function unbrokenRuns(points: readonly SeriesPoint[]): Placed[][] | null {
  const runs: Placed[][] = [];
  let previous: number | null = null;
  for (const point of points) {
    const value = axisPrice(point.close_usd);
    if (value === null || (previous !== null && point.at_ms <= previous)) return null;
    if (previous === null || point.at_ms - previous > BAR_MS) runs.push([]);
    runs[runs.length - 1].push({ point, value });
    previous = point.at_ms;
  }
  return runs;
}

/** A contract's name in words from the parsed fields the server sent for it; the symbol itself when the server sent none. */
export function contractWords(symbol: string, known: ReadonlyMap<string, TradeContract>): string {
  const contract = known.get(symbol);
  return contract === undefined ? symbol : optionContractName(contractOf(contract));
}

/** The facts the tooltip needs for one minute, collected as the plan is made. */
class TipTable {
  private readonly lines = new Map<number, string[]>();

  add(time: number, line: string): void {
    const at = this.lines.get(time);
    if (at === undefined) this.lines.set(time, [line]);
    else at.push(line);
  }

  done(): ReadonlyMap<number, readonly string[]> {
    return this.lines;
  }
}

function money(text: string): string {
  return formatAuthoredUsd(text) ?? text;
}

export function buildTimelinePlan(session: SessionDetail): TimelinePlan {
  const known = knownContracts(session);
  const grid = gridOf(session.session_open_ms, session.session_close_ms);
  const tips = new TipTable();
  const series: SeriesPlan[] = [];
  const panes: PanePlan[] = [];
  const notDrawn: NotDrawn[] = [];
  const paneOf = new Map<string, number>();
  const entriesOf: LegendEntry[][] = [];

  const listedBelow = (symbol: string): boolean =>
    session.levels.some((level) => level.contract === symbol)
    || session.fills.some((fill) => fill.contract === symbol)
    || session.marks.some((mark) => mark.contract === symbol);

  /**
   * Adds a shown series as a pane: one line series for each unbroken run, and a tip for every minute of the grid.
   * Returns whether the series was drawn; when it was not, `notDrawn` says why.
   */
  const addCloses = (source: SessionSeries, caption: string, token: ChartSeriesColorToken, key: string, ofUnderlying: boolean): boolean => {
    const missing = (reason: SeriesReason | null, message: string): false => {
      notDrawn.push({ key, what: null, symbol: source.symbol, ofUnderlying, contract: source.contract ?? null, reason, message, listedBelow: listedBelow(source.symbol) });
      return false;
    };
    if (source.state !== 'shown') {
      // The server's words, shown as it sent them; the page's own sentence only when it sent none.
      const sent = source.message ?? '';
      return missing(source.reason ?? null, sent.trim() === '' ? NO_REASON_GIVEN : sent);
    }
    if (source.points.length === 0) return missing(null, NO_MINUTES);
    const runs = unbrokenRuns(source.points);
    if (runs === null) return missing(null, UNPLACEABLE);
    const pane = panes.length;
    panes.push({ key, caption, symbol: source.symbol, contract: source.contract ?? null, entries: [] });
    const entries: LegendEntry[] = [{ key: `${key}:closes`, name: 'Minute closes', drawn: 'solid line', token, sample: 'line', lineStyle: 'solid', glyph: '' }];
    // A minute that traded alone is a line of one point; the library draws that as a stroke a few pixels long, so it gets a dot, said in the legend.
    if (runs.some((run) => run.length === 1)) entries.push({ key: `${key}:alone`, name: 'A lone minute with a trade', drawn: 'dot', token, sample: 'marker', lineStyle: 'solid', glyph: '●' });
    entriesOf.push(entries);
    paneOf.set(source.symbol, pane);
    runs.forEach((run, at) => {
      series.push({
        id: `${key}:closes:${at}`,
        role: 'closes',
        pane,
        token,
        lineStyle: 'solid',
        lineWidth: 2,
        lineVisible: true,
        pointMarkers: run.length === 1,
        data: run.map(({ point, value }) => ({ time: seconds(point.at_ms), value })),
        markers: [],
      });
    });
    const traded = new Map(source.points.map((point) => [seconds(point.at_ms), point.close_usd]));
    for (const time of grid) {
      const close = traded.get(time);
      tips.add(time, close === undefined ? `${caption}: no trade this minute` : `${caption}: ${money(close)}`);
    }
    return true;
  };

  const underlyingDrawn = addCloses(session.underlying, session.underlying.symbol, TOKENS.underlying, session.underlying.symbol, true);
  for (const each of session.contracts) {
    addCloses(each, contractWords(each.symbol, known), TOKENS.contract, each.symbol, false);
  }

  const drawnMarkers = addFills(session, known, paneOf, series, entriesOf, notDrawn, tips);
  const drawnLevels = addLevels(session, known, paneOf, grid, series, entriesOf, notDrawn, tips);

  // The clock goes first so the library has a slot for every minute before any line asks for one.
  if (panes.length > 0 && grid.length > 0) {
    series.unshift({ id: 'clock', role: 'clock', pane: 0, token: TOKENS.underlying, lineStyle: 'solid', lineWidth: 1, lineVisible: false, pointMarkers: false, data: grid.map((time) => ({ time })), markers: [] });
  }

  const finishedPanes = panes.map((pane, at) => ({ ...pane, entries: entriesOf[at] }));
  const drawn: Drawn = { underlying: underlyingDrawn, contracts: finishedPanes.length - (underlyingDrawn ? 1 : 0), fills: drawnMarkers.fills, marks: drawnMarkers.marks, levels: drawnLevels };
  return { panes: finishedPanes, series, notDrawn, tips: tips.done(), label: labelOf(session, drawn, notDrawn) };
}

/** What the chart draws, counted from what was placed on it and not from what the server sent. */
interface Drawn {
  readonly underlying: boolean;
  readonly contracts: number;
  readonly fills: number;
  readonly marks: number;
  readonly levels: number;
}

/**
 * The chart's name for those who cannot see it. It counts what is drawn, and says apart how many of the day's series,
 * fills, marks and levels are not, so that nothing is announced that the picture does not hold. The counts are written
 * as a label and a number, not as a noun that would need a plural.
 */
function labelOf(session: SessionDetail, drawn: Drawn, notDrawn: readonly NotDrawn[]): string {
  if (!drawn.underlying && drawn.contracts === 0) return 'Nothing could be drawn for this day.';
  const what = drawn.underlying ? `Minute closes of ${session.underlying.symbol}.` : `Minute closes; the underlying ${session.underlying.symbol} is not drawn.`;
  const counts = `Contracts drawn: ${drawn.contracts}. Fills drawn: ${drawn.fills}. Marks drawn: ${drawn.marks}. Levels drawn: ${drawn.levels}.`;
  const series = notDrawn.filter((item) => item.what === null).length;
  const fills = session.fills.length - drawn.fills;
  const marks = session.marks.length - drawn.marks;
  const levels = session.levels.length - drawn.levels;
  const apart = series + fills + marks + levels === 0 ? '' : ` Not drawn: series ${series}, fills ${fills}, marks ${marks}, levels ${levels}.`;
  return `${what} ${counts}${apart} The same facts are listed below as text.`;
}

/** Fills and marks: markers on a series of no line, one for each instant, in the pane of their contract. */
function addFills(
  session: SessionDetail,
  known: ReadonlyMap<string, TradeContract>,
  paneOf: ReadonlyMap<string, number>,
  series: SeriesPlan[],
  entriesOf: LegendEntry[][],
  notDrawn: NotDrawn[],
  tips: TipTable,
): { readonly fills: number; readonly marks: number } {
  const byPane = new Map<number, { markers: PlanMarker[]; points: Map<number, number> }>();
  let fills = 0;
  let marks = 0;
  const hostOf = (pane: number) => {
    let host = byPane.get(pane);
    if (host === undefined) {
      host = { markers: [], points: new Map() };
      byPane.set(pane, host);
    }
    return host;
  };
  const place = (pane: number, marker: PlanMarker, entry: LegendEntry): void => {
    const host = hostOf(pane);
    host.markers.push(marker);
    if (!host.points.has(marker.time)) host.points.set(marker.time, marker.price);
    if (!entriesOf[pane].some((each) => each.key === entry.key)) entriesOf[pane].push(entry);
  };

  for (const fill of session.fills) {
    const pane = paneOf.get(fill.contract);
    // A contract whose series is not drawn has no pane: nothing of it goes on the chart or into its tooltip.
    if (pane === undefined) continue;
    const words = contractWords(fill.contract, known);
    const kind = FILL_WORDS[fill.kind];
    const why = fill.exit_reason == null ? '' : ` (${formatReceiptLabel(fill.exit_reason)})`;
    tips.add(seconds(fill.bar_close_ms), `${kind.name} ${words} (contracts: ${fill.contracts}): filled ${money(fill.filled_usd)}, seen ${money(fill.seen_usd)}${why}`);
    const price = axisPrice(fill.filled_usd);
    if (price === null) {
      notDrawn.push({ key: fillKey(fill), what: kind.name, symbol: fill.contract, ofUnderlying: false, contract: known.get(fill.contract) ?? null, reason: null, message: UNPLACEABLE, listedBelow: true });
      continue;
    }
    fills += 1;
    const entry = fill.kind === 'entry';
    place(
      pane,
      { id: fillKey(fill), time: seconds(fill.bar_close_ms), price, shape: entry ? 'arrowUp' : 'arrowDown', token: entry ? TOKENS.entry : TOKENS.exit, text: kind.name },
      { key: `fill:${fill.kind}`, name: kind.name, drawn: kind.drawn, token: entry ? TOKENS.entry : TOKENS.exit, sample: 'marker', lineStyle: 'solid', glyph: entry ? '▲' : '▼' },
    );
  }

  for (const mark of session.marks) {
    const pane = paneOf.get(mark.contract);
    if (pane === undefined) continue;
    const words = contractWords(mark.contract, known);
    tips.add(seconds(mark.bar_close_ms), `${MARK_WORDS.name}: ${words} at ${money(mark.price_usd)}`);
    const price = axisPrice(mark.price_usd);
    if (price === null) {
      notDrawn.push({ key: markKey(mark), what: MARK_WORDS.name, symbol: mark.contract, ofUnderlying: false, contract: known.get(mark.contract) ?? null, reason: null, message: UNPLACEABLE, listedBelow: true });
      continue;
    }
    marks += 1;
    place(
      pane,
      { id: markKey(mark), time: seconds(mark.bar_close_ms), price, shape: 'square', token: TOKENS.mark, text: MARK_WORDS.name },
      { key: 'mark', name: MARK_WORDS.name, drawn: MARK_WORDS.drawn, token: TOKENS.mark, sample: 'marker', lineStyle: 'solid', glyph: '■' },
    );
  }

  for (const [pane, host] of [...byPane].sort(([left], [right]) => left - right)) {
    series.push({
      id: `pane-${pane}:fills`,
      role: 'fills',
      pane,
      token: TOKENS.mark,
      lineStyle: 'solid',
      lineWidth: 1,
      lineVisible: false,
      pointMarkers: false,
      data: [...host.points].sort(([left], [right]) => left - right).map(([time, value]) => ({ time, value })),
      markers: host.markers,
    });
  }
  return { fills, marks };
}

const fillKey = (fill: SessionFill): string => `fill:${fill.contract}:${fill.leg_id}:${fill.entry_number}:${fill.kind}`;
const markKey = (mark: SessionMark): string => `mark:${mark.contract}:${mark.leg_id}:${mark.entry_number}`;
const levelKey = (level: SessionLevel): string => `level:${level.contract}:${level.leg_id}:${level.entry_number}:${level.kind}`;

/** Levels: a line from the instant the server gave to the other it gave, and nowhere else, in the pane of its contract. */
function addLevels(
  session: SessionDetail,
  known: ReadonlyMap<string, TradeContract>,
  paneOf: ReadonlyMap<string, number>,
  grid: readonly number[],
  series: SeriesPlan[],
  entriesOf: LegendEntry[][],
  notDrawn: NotDrawn[],
  tips: TipTable,
): number {
  let drawn = 0;
  for (const level of session.levels) {
    const pane = paneOf.get(level.contract);
    if (pane === undefined) continue;
    const kind = LEVEL_WORDS[level.kind];
    const drawing = LEVEL_DRAWING[level.kind];
    const from = seconds(level.from_ms);
    const to = seconds(level.to_ms);
    // The tooltip names a level at each minute it is in force, in the server's own text.
    for (const time of grid) {
      if (time >= from && time <= to) tips.add(time, `${kind.name} ${money(level.price_usd)} (${legAttempt(level.leg_id, level.entry_number)})`);
    }
    const value = axisPrice(level.price_usd);
    if (value === null || level.to_ms < level.from_ms) {
      notDrawn.push({ key: levelKey(level), what: kind.name, symbol: level.contract, ofUnderlying: false, contract: known.get(level.contract) ?? null, reason: null, message: UNPLACEABLE, listedBelow: true });
      continue;
    }
    drawn += 1;
    series.push({
      id: levelKey(level),
      role: 'level',
      pane,
      token: drawing.token,
      lineStyle: drawing.style,
      lineWidth: 1,
      lineVisible: true,
      pointMarkers: false,
      data: from === to ? [{ time: from, value }] : [{ time: from, value }, { time: to, value }],
      markers: [],
    });
    if (!entriesOf[pane].some((each) => each.key === `level:${level.kind}`)) {
      entriesOf[pane].push({ key: `level:${level.kind}`, name: kind.name, drawn: kind.drawn, token: drawing.token, sample: 'line', lineStyle: drawing.style, glyph: '' });
    }
  }
  return drawn;
}
