/**
 * View models for the Compare step and the parameter map (#2696). They only
 * arrange, label and format values the server computed: which candidates are
 * the same settings (`same_as`), each cell's status and metrics, the flags.
 * No metric is derived here; a fill depth is a display scale of the server's
 * returns, never a number shown.
 */

import { knobsByName, metricTexts, pointEntries, signedPercentText, type MetricTexts } from './golden-search-display';
import type { CandidateKey, CapabilityKnob, EvidenceCandidate, Finding, Metrics, PairMap, PairMapCell, Point, PointValue, StrategyCapability } from './golden-search.types';

/** Candidates read the searches first and the frozen incumbent last. */
const CANDIDATE_ORDER: readonly CandidateKey[] = ['all_period', 'recent', 'incumbent'];

/** Where each candidate came from, in operator copy. */
export const CANDIDATE_ORIGINS: Readonly<Record<CandidateKey, string>> = {
  all_period: 'All-period search',
  recent: 'Most recent training window',
  incumbent: 'Frozen incumbent',
};

/** Flags that belong beside a metric rather than under the candidate's name. */
const DRAWDOWN_FLAGS: ReadonlySet<string> = new Set(['DRAWDOWN_ABOVE_CEILING', 'DRAWDOWN_UNDEFINED']);
const TRADE_FLAGS: ReadonlySet<string> = new Set(['TOO_FEW_TRADES', 'NO_TRADES']);

export interface CandidateRow extends MetricTexts {
  readonly key: CandidateKey;
  readonly candidate: EvidenceCandidate;
  readonly origin: string;
  /** Every candidate folded into this row, the representative included. */
  readonly members: readonly CandidateKey[];
  /** The labels of other candidates that are exactly these settings, folded into this row. */
  readonly sameAs: readonly string[];
  readonly drawdownFlags: readonly Finding[];
  readonly tradeFlags: readonly Finding[];
  readonly otherFlags: readonly Finding[];
}

/**
 * One row per distinct setting. Candidates the server marks `same_as` fold
 * into one row; when the incumbent is among them the row is the incumbent's,
 * because testing the current settings against themselves proves nothing.
 */
export function candidateRows(candidates: readonly EvidenceCandidate[]): CandidateRow[] {
  const byKey = new Map(candidates.map((candidate) => [candidate.key, candidate]));
  const ordered = CANDIDATE_ORDER.filter((key) => byKey.has(key));
  const placed = new Set<CandidateKey>();
  const rows: CandidateRow[] = [];
  for (const key of ordered) {
    if (placed.has(key)) continue;
    const group = new Set<CandidateKey>([key, ...(byKey.get(key)?.same_as ?? [])]);
    for (const other of ordered) if (byKey.get(other)?.same_as.includes(key)) group.add(other);
    const members = ordered.filter((member) => group.has(member));
    members.forEach((member) => placed.add(member));
    const representative = group.has('incumbent') ? 'incumbent' : members[0];
    const candidate = byKey.get(representative);
    if (candidate === undefined) continue;
    rows.push({
      key: representative,
      candidate,
      origin: CANDIDATE_ORIGINS[representative],
      members,
      sameAs: members.filter((member) => member !== representative).map((member) => byKey.get(member)?.label ?? member),
      ...metricTexts(candidate.development_metrics),
      drawdownFlags: candidate.flags.filter((flag) => DRAWDOWN_FLAGS.has(flag.code)),
      tradeFlags: candidate.flags.filter((flag) => TRADE_FLAGS.has(flag.code)),
      otherFlags: candidate.flags.filter((flag) => !DRAWDOWN_FLAGS.has(flag.code) && !TRADE_FLAGS.has(flag.code)),
    });
  }
  // A row represented by the incumbent still reads after the searches.
  return rows.sort((a, b) => CANDIDATE_ORDER.indexOf(a.key) - CANDIDATE_ORDER.indexOf(b.key));
}

/** The row a candidate key lives in after folding (`recent` may live in the all-period row). */
export function rowKeyFor(rows: readonly CandidateRow[], key: CandidateKey | null): CandidateKey | null {
  if (key === null) return null;
  const row = rows.find((candidate) => candidate.members.includes(key));
  return row?.key ?? null;
}

/** The candidate a study opens Compare on: its locked pick, else the all-period fit, else the first row. */
export function initialRowKey(rows: readonly CandidateRow[], locked: CandidateKey | null): CandidateKey | null {
  return rowKeyFor(rows, locked) ?? rowKeyFor(rows, 'all_period') ?? rows[0]?.key ?? null;
}

/** `Selected: <label>. <params>. <fixed>.` — the server's sentences, joined. */
export function selectedCaption(candidate: EvidenceCandidate): { label: string; detail: string } {
  const sentences = [candidate.params_sentence, candidate.fixed_sentence].filter((text) => text.trim() !== '');
  return { label: `Selected: ${candidate.label}.`, detail: sentences.map((text) => (/[.!?]$/.test(text) ? text : `${text}.`)).join(' ') };
}

// ---------------------------------------------------------------- parameter map

export type PairCellKind = 'tested' | 'failed' | 'invalid' | 'outside_domain' | 'untested';

export interface PairCellView {
  readonly key: string;
  readonly row: number;
  readonly column: number;
  readonly x: number;
  readonly y: number;
  readonly kind: PairCellKind;
  /** What the cell shows: the net return, or a status mark. */
  readonly text: string;
  /** The same in words, for the table alternative. */
  readonly words: string;
  readonly ariaLabel: string;
  /** A CSS background for a tested cell: deeper means a higher return (display scale only). */
  readonly fill: string | null;
  readonly loss: boolean;
  readonly center: boolean;
  readonly metrics: Metrics | null;
  readonly reason: string | null;
}

/** A pair map by name: the pair, and its knobs' declared labels. */
interface PairNames {
  readonly id: string;
  readonly title: string;
  readonly xLabel: string;
  readonly yLabel: string;
}

export interface PairMapView extends PairNames {
  readonly unitNote: string;
  readonly xValues: readonly number[];
  readonly yValues: readonly number[];
  readonly rows: readonly (readonly PairCellView[])[];
  readonly heldFixed: string;
}

/** A pair audit's name and how many of its grid positions were valid (tested or not) or ruled out. */
export interface PairAudit extends PairNames {
  readonly valid: number;
  readonly invalid: number;
}

const STATUS_MARKS: Readonly<Record<Exclude<PairCellKind, 'tested'>, string>> = {
  failed: 'Failed',
  invalid: '—',
  outside_domain: '·',
  untested: '?',
};

const STATUS_WORDS: Readonly<Record<Exclude<PairCellKind, 'tested'>, string>> = {
  failed: 'Run failed',
  invalid: 'Invalid',
  outside_domain: 'Outside range',
  untested: 'Not tested',
};

function numberOf(value: PointValue | undefined): number | null {
  return typeof value === 'number' ? value : null;
}

function cellKind(cell: PairMapCell | undefined): PairCellKind {
  if (cell === undefined) return 'untested';
  if (cell.status === 'center') return 'tested';
  if (cell.status === 'tested' && cell.metrics?.status === 'failed') return 'failed';
  return cell.status;
}

/** Fill depth for a tested return: 14–48% of the gain colour by share of the map's best gain; losses get one amber tint. */
function fillFor(value: number | null, bestGain: number): { fill: string | null; loss: boolean } {
  if (value === null) return { fill: null, loss: false };
  if (value < 0) return { fill: 'color-mix(in srgb, var(--warn) 22%, var(--bg-surface))', loss: true };
  const share = bestGain > 0 ? value / bestGain : 0;
  return { fill: `color-mix(in srgb, var(--bull) ${Math.round(14 + 34 * share)}%, var(--bg-surface))`, loss: false };
}

function pairNames(map: PairMap, knobs: ReadonlyMap<string, CapabilityKnob>): PairNames {
  const xLabel = knobs.get(map.x_knob)?.label ?? map.x_knob;
  const yLabel = knobs.get(map.y_knob)?.label ?? map.y_knob;
  return { id: `${map.y_knob}-${map.x_knob}`, title: `${yLabel} × ${xLabel}`, xLabel, yLabel };
}

/** A pair audit's counts from the server's cells: every grid position not ruled out is valid, tested or not. */
export function pairAudit(map: PairMap, capability: StrategyCapability | null): PairAudit {
  const invalid = map.cells.filter((cell) => cell.status === 'invalid' || cell.status === 'outside_domain').length;
  return { ...pairNames(map, knobsByName(capability)), valid: map.x_values.length * map.y_values.length - invalid, invalid };
}

/**
 * The parameter map as the screen lays it out: rows are `y_knob`'s values
 * (top to bottom), columns `x_knob`'s, every cell named for a screen reader.
 * `center` is the candidate the map is drawn through; its cell is marked.
 */
export function pairMapView(map: PairMap, capability: StrategyCapability | null, center: Point | null): PairMapView {
  const knobs = knobsByName(capability);
  const xKnob = knobs.get(map.x_knob);
  const yKnob = knobs.get(map.y_knob);
  const names = pairNames(map, knobs);
  const { xLabel, yLabel } = names;
  const lookup = new Map(map.cells.map((cell) => [`${cell.y}|${cell.x}`, cell]));
  const valueAt = (name: string): number | null => (center === null ? null : (numberOf(center[name]) ?? knobs.get(name)?.default_value ?? null));
  const centerX = valueAt(map.x_knob);
  const centerY = valueAt(map.y_knob);
  const gains = map.cells.flatMap((cell) => (cellKind(cell) === 'tested' && (cell.metrics?.total_return_pct ?? 0) > 0 ? [cell.metrics?.total_return_pct ?? 0] : []));
  const bestGain = gains.length === 0 ? 0 : Math.max(...gains);
  const rows = map.y_values.map((y, row) =>
    map.x_values.map((x, column): PairCellView => {
      const cell = lookup.get(`${y}|${x}`);
      const kind = cellKind(cell);
      const where = `${yLabel} ${y}, ${xLabel} ${x}`;
      const reason = cell?.reason ?? null;
      const metrics = cell?.metrics ?? null;
      const value = kind === 'tested' ? (metrics?.total_return_pct ?? null) : null;
      const shade = fillFor(value, bestGain);
      const text = kind === 'tested' ? signedPercentText(value) : STATUS_MARKS[kind];
      const words = kind === 'tested' ? text : STATUS_WORDS[kind];
      const ariaLabel =
        kind === 'tested'
          ? `${where}, development net return ${signedPercentText(value)}`
          : kind === 'failed'
            ? `${where}, the run failed`
            : kind === 'invalid'
              ? `Invalid: ${where}${reason ? ` — ${reason}` : ''}`
              : kind === 'outside_domain'
                ? `Outside the legal range: ${where}`
                : `Not tested: ${where}`;
      return { key: `${y}|${x}`, row, column, x, y, kind, text, words, ariaLabel, ...shade, center: x === centerX && y === centerY, metrics, reason };
    }),
  );
  const held = center === null ? [] : pointEntries(center, capability).filter((entry) => entry.name !== map.x_knob && entry.name !== map.y_knob);
  const unitNote = xKnob && yKnob && xKnob.unit === yKnob.unit ? `Both in ${xKnob.unit}` : `${yKnob?.unit ?? ''} ↓ · ${xKnob?.unit ?? ''} →`;
  return {
    ...names,
    unitNote,
    xValues: map.x_values,
    yValues: map.y_values,
    rows,
    heldFixed: held.map((entry) => `${entry.label} ${entry.value ?? '—'}`).join(' · '),
  };
}

/** The cell a map opens on: the candidate's own cell when it was tested, else the first tested cell. */
export function initialCell(view: PairMapView): PairCellView | null {
  const cells = view.rows.flat();
  return cells.find((cell) => cell.center && cell.kind === 'tested') ?? cells.find((cell) => cell.kind === 'tested') ?? cells[0] ?? null;
}

/** The selected cell in one line: where it is and what it earned, or why it has no result. */
export function cellDetail(view: PairMapView, cell: PairCellView): string {
  const where = `${view.yLabel} ${cell.y} · ${view.xLabel} ${cell.x}`;
  const m = cell.metrics;
  if (cell.kind === 'tested' && m !== null) {
    const figures = metricTexts(m);
    return `${where} · ${figures.netReturn} net return · Sharpe ${figures.sharpe} · ${figures.trades} trades · worst fall ${figures.worstFall}`;
  }
  if (cell.kind === 'failed') return `${where} · the run failed${m?.error ? `: ${m.error}` : ''}`;
  if (cell.kind === 'invalid') return `${where} · not a valid pair${cell.reason ? `: ${cell.reason}` : ''}`;
  if (cell.kind === 'outside_domain') return `${where} · outside the legal range, not tested`;
  return `${where} · not tested`;
}
