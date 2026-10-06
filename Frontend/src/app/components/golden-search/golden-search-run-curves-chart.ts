import type { ChartSpec } from './charts/golden-search-chart-spec';
import { NOT_RECORDED, signedPercentOrNotRecorded, tooltipHtml, type ChartTheme } from './charts/golden-search-chart-theme';
import { percentText, signedPercentText } from './golden-search-display';
import { alignToSessions, lastValue, sessionAxis, sessionCurvesOption, type SessionLine } from './golden-search-session-curves';
import type { RunCurve } from './golden-search.types';

/** One run to draw: what asked for it, its settings in words, and its curve as the server sent it. */
export interface RunLine {
  readonly key: string;
  readonly label: string;
  /** The run's settings sentence: with the label it tells any two runs apart. */
  readonly settings: string;
  readonly curve: RunCurve;
}

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
 */
export function runCurvesSpec(lines: readonly RunLine[], ceiling: number): ChartSpec {
  const { sessions, dates } = sessionAxis(lines.map((line) => line.curve.close_ms));
  const aligned: AlignedRun[] = lines.map((line) => ({
    line,
    returns: alignToSessions(sessions, line.curve.close_ms, line.curve.cumulative_return),
    falls: line.curve.drawdown === null ? sessions.map(() => null) : alignToSessions(sessions, line.curve.close_ms, line.curve.drawdown),
  }));
  const sessionDate = (line: RunLine, at: 0 | -1): string => {
    const ms = line.curve.close_ms.at(at);
    return ms === undefined ? '—' : dates[sessions.indexOf(ms)];
  };
  return {
    label: 'Run cumulative return and fall from peak',
    summary: `${lines.length} run${lines.length === 1 ? '' : 's'} drawn at every session close; beneath, each run's fall from its own peak.`,
    featured: lines[0]?.key ?? null,
    option: (theme) => sessionCurvesOption(drawn(aligned, theme), dates, ceiling, (index) => tooltip(aligned, dates[index], index, ceiling, theme), theme),
    table: {
      caption: 'Each drawn run: its settings, where it ended and its deepest fall at a session close; — marks a value the run did not record',
      columns: ['Run', 'Settings', 'First session', 'Last session', 'Ended at', 'Deepest fall'],
      rows: aligned.map(({ line, returns }) => ({
        key: line.key,
        cells: [line.label, line.settings, sessionDate(line, 0), sessionDate(line, -1), signedPercentText(lastValue(returns)), signedPercentText(line.curve.deepest_drawdown)],
      })),
    },
  };
}

function colorOf(index: number, theme: ChartTheme): string {
  return theme.series[index % theme.series.length];
}

/** Each run in the next palette colour; a crowd of them thins out and loses its end labels. */
function drawn(aligned: readonly AlignedRun[], theme: ChartTheme): SessionLine[] {
  const crowded = aligned.length > END_LABELS;
  return aligned.map(({ line, returns, falls }, index) => ({
    key: line.key,
    name: line.label,
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

function tooltip(aligned: readonly AlignedRun[], date: string, index: number, ceiling: number, theme: ChartTheme): string {
  // The runs that scored this session, best return first.
  const scored = aligned.map((run, at) => ({ run, at })).filter(({ run }) => run.returns[index] !== null);
  scored.sort((a, b) => (b.run.returns[index] ?? 0) - (a.run.returns[index] ?? 0));
  const shown = scored.slice(0, HOVER_ROWS);
  const notes = [`Worst-fall limit ${percentText(ceiling)}. The rules judge each run’s bar-by-bar fall, which can be deeper than this session-close line.`];
  if (scored.length > shown.length) notes.unshift(`The ${shown.length} best of ${scored.length} runs at this session. Tick fewer runs to read them all.`);
  if (scored.length < aligned.length) notes.push(`${aligned.length - scored.length} drawn run${aligned.length - scored.length === 1 ? '' : 's'} did not score this session.`);
  return tooltipHtml(
    {
      title: `${date} · at the session close`,
      columns: ['Return', 'Fall from peak'],
      rows: shown.map(({ run, at }) => ({
        label: `${run.line.label} · ${run.line.settings}`,
        swatch: { color: colorOf(at, theme), dashed: false },
        values: [signedPercentOrNotRecorded(run.returns[index]), run.line.curve.drawdown === null ? NOT_RECORDED : signedPercentOrNotRecorded(run.falls[index])],
      })),
      notes,
    },
    theme,
  );
}
