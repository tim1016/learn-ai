import type { ChartSpec } from './charts/golden-search-chart-spec';
import { NOT_RECORDED, signedPercentOrNotRecorded, tooltipHtml, type ChartTheme } from './charts/golden-search-chart-theme';
import { percentText, signedPercentText } from './golden-search-display';
import { alignToSessions, lastValue, sessionAxis, sessionCurvesOption, type SessionLine } from './golden-search-session-curves';
import type { CandidateKey, CurveLine, LineRole } from './golden-search.types';

interface AlignedLine {
  readonly line: CurveLine;
  readonly returns: readonly (number | null)[];
  readonly falls: readonly (number | null)[];
}

/** A hover lists this many lines: the named ones first, then a search's other points, best first. */
const HOVER_ROWS = 12;
/** Each named line carries its end value at the right edge only while they do not pile up. */
const END_LABELS = 8;

/**
 * One step's runs on the shared session chart (#2930), each drawn by the
 * role the server gave it: the set's winner or candidate bold in `accent`'s
 * colour, the current settings dashed, a cost stress and a neighbor in their
 * own colours, and a search's other points faint behind them all. A faint
 * line is named by its settings, since the server calls every one of them
 * "Scored point". Every value is the server's.
 */
export function stepCurvesSpec(lines: readonly CurveLine[], ceiling: number, accent: CandidateKey): ChartSpec {
  const { sessions, dates } = sessionAxis(lines.map((line) => line.curve.close_ms));
  const aligned: AlignedLine[] = lines.map((line) => ({
    line,
    returns: alignToSessions(sessions, line.curve.close_ms, line.curve.cumulative_return),
    falls: line.curve.drawdown === null ? sessions.map(() => null) : alignToSessions(sessions, line.curve.close_ms, line.curve.drawdown),
  }));
  const named = lines.filter((line) => line.role !== 'other').length;
  return {
    label: 'Run cumulative return and fall from peak',
    summary: `${lines.length} run${lines.length === 1 ? '' : 's'} drawn at every session close, ${named} of them named; beneath, each run's fall from its own peak.`,
    featured: (lines.find((line) => line.role === 'winner' || line.role === 'candidate') ?? lines[0])?.curve.evaluation_key ?? null,
    option: (theme) => sessionCurvesOption(drawn(aligned, accent, named <= END_LABELS, theme), dates, ceiling, (index) => tooltip(aligned, dates[index], index, ceiling, accent, theme), theme),
    table: {
      caption: 'Each drawn run: its settings, where it ended and its deepest fall at a session close; — marks a value the run did not record',
      columns: ['Line', 'Settings', 'Ended at', 'Deepest fall'],
      rows: aligned.map(({ line, returns }) => ({
        key: line.curve.evaluation_key,
        cells: [line.label, line.params_sentence, signedPercentText(lastValue(returns)), signedPercentText(line.curve.deepest_drawdown)],
      })),
    },
  };
}

interface RoleStyle {
  readonly color: string;
  readonly width: number;
  readonly opacity?: number;
  readonly z: number;
}

function styleOf(role: LineRole, accent: CandidateKey, theme: ChartTheme): RoleStyle {
  switch (role) {
    case 'winner':
    case 'candidate':
      return { color: theme.candidates[accent], width: 2.5, z: 5 };
    case 'incumbent':
      return { color: theme.candidates.incumbent, width: 1.75, z: 4 };
    case 'stress':
      return { color: theme.stressed, width: 1.5, z: 3 };
    case 'neighbor':
      return { color: theme.stepBelow, width: 1.25, opacity: 0.9, z: 3 };
    case 'other':
      return { color: theme.textSecondary, width: 1, opacity: 0.3, z: 1 };
  }
}

function drawn(aligned: readonly AlignedLine[], accent: CandidateKey, labelled: boolean, theme: ChartTheme): SessionLine[] {
  return aligned.map(({ line, returns, falls }) => {
    const faint = line.role === 'other';
    // The current settings are dashed wherever they are drawn, also when they are the candidate the chart is about.
    const dashed = line.role === 'incumbent' || (line.role === 'candidate' && accent === 'incumbent');
    return { key: line.curve.evaluation_key, name: line.label, returns, falls, ...styleOf(line.role, accent, theme), dashed, endLabel: labelled && !faint, filled: false, quiet: faint };
  });
}

function nameOf(line: CurveLine): string {
  return line.role === 'other' ? line.params_sentence : `${line.label} · ${line.params_sentence}`;
}

function tooltip(aligned: readonly AlignedLine[], date: string, index: number, ceiling: number, accent: CandidateKey, theme: ChartTheme): string {
  // The lines that scored this session: the named ones first, then the other points by return.
  const scored = aligned.filter((run) => run.returns[index] !== null);
  const faint = (run: AlignedLine): number => (run.line.role === 'other' ? 1 : 0);
  const ordered = [...scored].sort((a, b) => faint(a) - faint(b) || (b.returns[index] ?? 0) - (a.returns[index] ?? 0));
  const shown = ordered.slice(0, HOVER_ROWS);
  const notes = [`Worst-fall limit ${percentText(ceiling)}. The rules judge each run’s bar-by-bar fall, which can be deeper than this session-close line.`];
  if (ordered.length > shown.length) notes.unshift(`${shown.length} of the ${ordered.length} runs at this session. All runs lists every one.`);
  return tooltipHtml(
    {
      title: `${date} · at the session close`,
      columns: ['Return', 'Fall from peak'],
      rows: shown.map((run) => ({
        label: nameOf(run.line),
        swatch: { color: styleOf(run.line.role, accent, theme).color, dashed: run.line.role === 'incumbent' },
        values: [signedPercentOrNotRecorded(run.returns[index]), run.line.curve.drawdown === null ? NOT_RECORDED : signedPercentOrNotRecorded(run.falls[index])],
      })),
      notes,
    },
    theme,
  );
}
