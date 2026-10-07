import type { ChartSpec } from '../golden-search/charts/golden-search-chart-spec';
import { NOT_RECORDED, signedPercentOrNotRecorded, tooltipHtml, type ChartTheme } from '../golden-search/charts/golden-search-chart-theme';
import { signedPercentText } from '../golden-search/golden-search-display';
import { lastValue, onOneAxis, recordedSpan, sessionCurvesOption, type SessionCurve, type SessionLine } from '../golden-search/golden-search-session-curves';

/** One fold to draw: its place in the study, its winner's settings in words, and its test-window curve as the server sent it. */
export interface FoldLine {
  readonly foldIndex: number;
  readonly settings: string;
  readonly curve: SessionCurve & { readonly deepest_drawdown: number | null };
}

interface DrawnFold {
  readonly fold: FoldLine;
  readonly name: string;
  readonly returns: readonly (number | null)[];
  readonly falls: readonly (number | null)[];
}

/** Each fold carries its end value at its last session only while the labels have room beside one another. */
const END_LABELS = 12;

/**
 * A study's folds on the shared session chart (#2936), one after another:
 * each fold's winner over its own test window, in the next palette colour,
 * with where it ended at its right end while there are few enough folds for
 * the labels to stand apart. `sessions` is the read's axis, every
 * fold's test window, so a fold with no line shows as a gap. Folds share no
 * session, so a hover names the one fold that scored it, or says none did.
 * Every value is the server's, the deepest fall included.
 */
export function foldCurvesSpec(sessions: readonly number[], folds: readonly FoldLine[]): ChartSpec {
  const axis = onOneAxis(
    folds.map((fold) => ({ sessions, curve: fold.curve })),
    sessions,
  );
  const dates = axis.dates;
  const drawn: DrawnFold[] = folds.map((fold, i) => ({ fold, name: `Fold ${fold.foldIndex + 1}`, ...axis.lines[i] }));
  const sessionDate = (returns: readonly (number | null)[], edge: 0 | 1): string => {
    const span = recordedSpan(returns);
    return span === null ? '—' : dates[span[edge]];
  };
  return {
    label: 'Fold cumulative return and fall from peak',
    summary: `${folds.length} fold${folds.length === 1 ? '' : 's'} drawn, each over its own test window at every session close; beneath, each fold's fall from its own peak.`,
    featured: folds.length === 0 ? null : keyOf(folds[0]),
    option: (theme) => sessionCurvesOption(lines(drawn, theme), dates, null, (index) => tooltip(drawn, dates[index], index, theme), theme),
    table: {
      caption: 'Each drawn fold: its winner’s settings, the first and last sessions of its test window, where it ended and its deepest fall at a session close',
      columns: ['Fold', 'Settings', 'First session', 'Last session', 'Ended at', 'Deepest fall'],
      rows: drawn.map(({ fold, name, returns }) => ({
        key: keyOf(fold),
        cells: [name, fold.settings, sessionDate(returns, 0), sessionDate(returns, 1), signedPercentText(lastValue(returns)), signedPercentText(fold.curve.deepest_drawdown)],
      })),
    },
  };
}

function keyOf(fold: FoldLine): string {
  return `fold-${fold.foldIndex}`;
}

function colorOf(index: number, theme: ChartTheme): string {
  return theme.series[index % theme.series.length];
}

/** Each fold at full strength: folds follow one another, so however many there are the lines never pile up. */
function lines(drawn: readonly DrawnFold[], theme: ChartTheme): SessionLine[] {
  const labelled = drawn.length <= END_LABELS;
  return drawn.map(({ fold, name, returns, falls }, index) => ({ key: keyOf(fold), name, returns, falls, color: colorOf(index, theme), width: 1.75, dashed: false, z: 2, endLabel: labelled, filled: false }));
}

function tooltip(drawn: readonly DrawnFold[], date: string, index: number, theme: ChartTheme): string {
  const scored = drawn.map((fold, at) => ({ fold, at })).filter(({ fold }) => fold.returns[index] !== null);
  return tooltipHtml(
    {
      title: `${date} · at the session close`,
      columns: ['Return', 'Fall from peak'],
      rows: scored.map(({ fold, at }) => ({
        label: `${fold.name} · ${fold.fold.settings}`,
        swatch: { color: colorOf(at, theme), dashed: false },
        values: [signedPercentOrNotRecorded(fold.returns[index]), fold.fold.curve.drawdown === null ? NOT_RECORDED : signedPercentOrNotRecorded(fold.falls[index])],
      })),
      notes: scored.length === 0 ? ['No fold has a curve at this session.'] : [],
    },
    theme,
  );
}
