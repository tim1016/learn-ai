import type { ChartSpec } from './charts/golden-search-chart-spec';
import { NOT_RECORDED, signedPercentOrNotRecorded, tooltipHtml, type ChartTheme } from './charts/golden-search-chart-theme';
import { DEVELOPMENT_NOTE, percentText, signedPercentText } from './golden-search-display';
import { alignToSessions, lastValue, sessionAxis, sessionCurvesOption, type SessionLine } from './golden-search-session-curves';
import type { CandidateKey, CumulativeReturnPoint, DrawdownPoint } from './golden-search.types';

/** One candidate's development detail run as the server sent it; both lists are empty when no run is recorded. */
export interface EquityLine {
  readonly key: CandidateKey;
  readonly label: string;
  readonly returns: readonly CumulativeReturnPoint[];
  readonly falls: readonly DrawdownPoint[];
}

interface AlignedLine {
  readonly line: EquityLine;
  readonly recorded: boolean;
  readonly returns: readonly (number | null)[];
  readonly falls: readonly (number | null)[];
}

/** The period an equity chart draws: its name, whose fall the limit judges, and the note every hover ends with. */
export interface EquityPeriod {
  readonly label: string;
  readonly judged: string;
  readonly note: string;
}

export const DEVELOPMENT_EQUITY: EquityPeriod = { label: 'Development cumulative return and fall from peak', judged: 'The rules judge each run’s bar-by-bar fall', note: DEVELOPMENT_NOTE };

/**
 * The equity chart (#2821): every run's cumulative return on top and its
 * fall from its own peak beneath, on one session axis — Compare's candidates
 * over the development period (V19), and the final test's two runs (V34). Both series are the server's, one value per session close;
 * a session a run did not record stays empty, never zero. The worst-fall
 * limit is drawn on the fall axis for reference: the rules judge the
 * engine's bar-by-bar fall, which can be deeper than these session closes.
 */
export function equityChartSpec(lines: readonly EquityLine[], featured: CandidateKey, ceiling: number, period: EquityPeriod = DEVELOPMENT_EQUITY): ChartSpec {
  const { sessions, dates } = sessionAxis(lines.flatMap((line) => [line.returns.map((p) => p.ms), line.falls.map((p) => p.ms)]));
  const aligned: AlignedLine[] = lines.map((line) => ({
    line,
    recorded: line.returns.length > 0,
    returns: alignToSessions(sessions, line.returns.map((p) => p.ms), line.returns.map((p) => p.value)),
    falls: alignToSessions(sessions, line.falls.map((p) => p.ms), line.falls.map((p) => p.drawdown)),
  }));
  const ends = aligned.map(({ line, recorded, returns }) => `${line.label} ${recorded ? signedPercentText(lastValue(returns)) : NOT_RECORDED}`);
  return {
    label: period.label,
    summary: `Ends at ${ends.join(', ')}; beneath, each line's fall from its own peak at every session close.`,
    featured,
    option: (theme) => sessionCurvesOption(drawn(aligned, featured, theme), dates, ceiling, (index) => tooltip(aligned, dates[index], index, ceiling, period, theme), theme),
    table: {
      caption: `${period.label} at each session close (ET); — marks a value the run did not record`,
      columns: ['Session', ...lines.flatMap((line) => [`${line.label} return`, `${line.label} fall from peak`])],
      rows: sessions.map((ms, i) => ({
        key: String(ms),
        cells: [dates[i], ...aligned.flatMap((a) => [signedPercentText(a.returns[i]), signedPercentText(a.falls[i])])],
      })),
    },
  };
}

/** The recorded lines as drawn: the featured one thicker and filled, the current settings dashed. */
function drawn(aligned: readonly AlignedLine[], featured: CandidateKey, theme: ChartTheme): SessionLine[] {
  return aligned
    .filter((a) => a.recorded)
    .map(({ line, returns, falls }) => {
      const isFeatured = line.key === featured;
      return { key: line.key, name: line.label, returns, falls, color: theme.candidates[line.key], width: isFeatured ? 2.5 : 1.5, dashed: line.key === 'incumbent', z: isFeatured ? 3 : 2, endLabel: true, filled: isFeatured };
    });
}

function tooltip(aligned: readonly AlignedLine[], date: string, index: number, ceiling: number, period: EquityPeriod, theme: ChartTheme): string {
  return tooltipHtml(
    {
      title: `${date} · at the session close`,
      columns: ['Return', 'Fall from peak'],
      rows: aligned.map(({ line, recorded, returns, falls }) => ({
        label: line.label,
        swatch: { color: theme.candidates[line.key], dashed: line.key === 'incumbent' },
        values: recorded ? [signedPercentOrNotRecorded(returns[index]), signedPercentOrNotRecorded(falls[index])] : [NOT_RECORDED, NOT_RECORDED],
      })),
      notes: [
        `Worst-fall limit ${percentText(ceiling)}. ${period.judged}, which can be deeper than this session-close line.`,
        period.note,
      ],
    },
    theme,
  );
}
