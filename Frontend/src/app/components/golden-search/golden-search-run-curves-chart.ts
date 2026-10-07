import type { ChartSpec } from './charts/golden-search-chart-spec';
import { NOT_RECORDED, signedPercentOrNotRecorded, tooltipHtml, type ChartTheme } from './charts/golden-search-chart-theme';
import { percentText, signedPercentText } from './golden-search-display';
import { lastValue, onOneAxis, recordedSpan, sessionCurvesOption, type SessionCurve, type SessionLine } from './golden-search-session-curves';

/** One run to draw: what it is called, its settings in words, and its curve as the server sent it. */
export interface RunLine {
  readonly key: string;
  /** What asked for the run, or what it is among the others; null for a run its settings alone name. */
  readonly label: string | null;
  /** The run's settings sentence: with the label it tells any two runs apart. */
  readonly settings: string;
  /** The session closes of the read that returned the curve. */
  readonly sessions: readonly number[];
  readonly curve: SessionCurve & { readonly deepest_drawdown: number | null };
}

/** What a page calls the runs it draws: a Golden Search study's are runs, a Grid Search's are cells. */
export interface RunNoun {
  readonly one: string;
  readonly many: string;
}

const RUNS: RunNoun = { one: 'run', many: 'runs' };

interface AlignedRun {
  readonly line: RunLine;
  readonly returns: readonly (number | null)[];
  readonly falls: readonly (number | null)[];
}

/** A hover lists this many runs, best first; past it the lines are a crowd, read by ticking fewer. */
const HOVER_ROWS = 12;
/** Each line carries its end value at the right edge only while they do not pile up. */
const END_LABELS = 8;

/**
 * The runs the owner ticked, drawn together (#2930) on the shared session
 * chart. Runs over different windows share the axis and each spans only its
 * own sessions. Every value is the server's, the deepest fall included.
 * `ceiling` is the page's worst-fall limit, null for a page with none.
 * `also` names sessions the axis holds though no run scored them.
 */
export function runCurvesSpec(lines: readonly RunLine[], ceiling: number | null, noun: RunNoun = RUNS, also: readonly number[] = []): ChartSpec {
  const axis = onOneAxis(lines, also);
  const dates = axis.dates;
  const aligned: AlignedRun[] = lines.map((line, i) => ({ line, ...axis.lines[i] }));
  const sessionDate = (returns: readonly (number | null)[], edge: 0 | 1): string => {
    const span = recordedSpan(returns);
    return span === null ? '—' : dates[span[edge]];
  };
  const titled = `${noun.one.charAt(0).toUpperCase()}${noun.one.slice(1)}`;
  return {
    label: `${titled} cumulative return and fall from peak`,
    summary: `${lines.length} ${lines.length === 1 ? noun.one : noun.many} drawn at every session close; beneath, each ${noun.one}'s fall from its own peak.`,
    featured: lines[0]?.key ?? null,
    option: (theme) => sessionCurvesOption(drawn(aligned, theme), dates, ceiling, (index) => tooltip(aligned, dates[index], index, ceiling, noun, theme), theme),
    table: {
      caption: `Each drawn ${noun.one}: its settings, where it ended and its deepest fall at a session close; — marks a value the ${noun.one} did not record`,
      columns: [titled, 'Settings', 'First session', 'Last session', 'Ended at', 'Deepest fall'],
      rows: aligned.map(({ line, returns }) => ({
        key: line.key,
        cells: [line.label ?? '—', line.settings, sessionDate(returns, 0), sessionDate(returns, 1), signedPercentText(lastValue(returns)), signedPercentText(line.curve.deepest_drawdown)],
      })),
    },
  };
}

function colorOf(index: number, theme: ChartTheme): string {
  return theme.series[index % theme.series.length];
}

function nameOf(line: RunLine): string {
  return line.label === null ? line.settings : `${line.label} · ${line.settings}`;
}

/** Each run in the next palette colour; a crowd of them thins out and loses its end labels. */
function drawn(aligned: readonly AlignedRun[], theme: ChartTheme): SessionLine[] {
  const crowded = aligned.length > END_LABELS;
  return aligned.map(({ line, returns, falls }, index) => ({
    key: line.key,
    name: line.label ?? line.settings,
    returns,
    falls,
    color: colorOf(index, theme),
    width: crowded ? 1 : 1.75,
    ...(crowded ? { opacity: 0.7 } : {}),
    dashed: false,
    z: 2,
    endLabel: !crowded,
    filled: false,
  }));
}

function tooltip(aligned: readonly AlignedRun[], date: string, index: number, ceiling: number | null, noun: RunNoun, theme: ChartTheme): string {
  // The runs that scored this session, best return first.
  const scored = aligned.map((run, at) => ({ run, at })).filter(({ run }) => run.returns[index] !== null);
  scored.sort((a, b) => (b.run.returns[index] ?? 0) - (a.run.returns[index] ?? 0));
  const shown = scored.slice(0, HOVER_ROWS);
  const notes = [
    ceiling === null
      ? `A ${noun.one}’s bar-by-bar worst fall can be deeper than this session-close line.`
      : `Worst-fall limit ${percentText(ceiling)}. The rules judge each ${noun.one}’s bar-by-bar fall, which can be deeper than this session-close line.`,
  ];
  if (scored.length > shown.length) notes.unshift(`The ${shown.length} best of ${scored.length} ${noun.many} at this session. Tick fewer ${noun.many} to read them all.`);
  const missing = aligned.length - scored.length;
  if (missing > 0) notes.push(`${missing} drawn ${missing === 1 ? noun.one : noun.many} did not score this session.`);
  return tooltipHtml(
    {
      title: `${date} · at the session close`,
      columns: ['Return', 'Fall from peak'],
      rows: shown.map(({ run, at }) => ({
        label: nameOf(run.line),
        swatch: { color: colorOf(at, theme), dashed: false },
        values: [signedPercentOrNotRecorded(run.returns[index]), run.line.curve.drawdown === null ? NOT_RECORDED : signedPercentOrNotRecorded(run.falls[index])],
      })),
      notes,
    },
    theme,
  );
}
