/**
 * The Plan step's problems in plain words (#2696), each pointing at the input
 * it belongs to: values the form cannot read yet, then the refusals the
 * server preflight returned. Every plan input carries the id these helpers
 * name, so the plan footer can take the trader straight to the field.
 */

import { etIsoDate } from '../../shared/date/et-midnight';
import { problemField, type KnobNumberField, type ProtocolNumberField } from './golden-search-plan-draft';
import type { BackfillRemedy, KnobPlan, ProtocolRefusal, StrategyCapability } from './golden-search.types';

export interface PlanProblem {
  readonly key: string;
  readonly text: string;
  /** The id of the input to fix, or null when no single input owns the problem. */
  readonly target: string | null;
  /** The days the study range could start or end on instead; empty when the server named none. */
  readonly offers: readonly RangeOffer[];
  /** The backfill that fetches the sessions the problem names as missing; null when none does. */
  readonly backfill: BackfillRemedy | null;
}

/** A day (`YYYY-MM-DD`) the server would take for one end of the study range. */
export interface RangeOffer {
  readonly edge: 'start' | 'end';
  readonly day: string;
}

/** What only a server refusal carries. */
const UNREFUSED = { offers: [], backfill: null } as const;

export const NUMBER_FIELD_LABELS: Readonly<Record<ProtocolNumberField, string>> = {
  min_trades: 'Min completed trades',
  expected_trades_per_year: 'Expected trade frequency',
  drawdown_percent: 'Max drawdown',
  commission_per_order: 'Flat fee per order',
  slippage_per_share: 'Slippage per share',
  initial_cash: 'Starting capital',
  training_months: 'Training window (months)',
  test_months: 'Test window (months)',
  final_months: 'Final test (months)',
  zoom_points: 'Points per round',
  zoom_refinements: 'Refinement rounds',
  zoom_passes: 'Max passes',
  exam_min_trades: 'Min final-test trades',
  budget_cap: 'Run cap',
};

const KNOB_FIELD_LABELS: Readonly<Record<KnobNumberField, string>> = {
  low: 'low end',
  high: 'high end',
  fixed_value: 'held value',
  step: 'step',
};

export function knobInputId(name: string, field: KnobNumberField): string {
  return `gs-plan-knob-${name}-${field}`;
}

export function importanceInputId(name: string): string {
  return `gs-plan-knob-${name}-importance`;
}

export function numberInputId(field: ProtocolNumberField): string {
  return `gs-plan-${field}`;
}

/** The study range picker: every date the plan carries is laid out from it. */
export const RANGE_INPUT_ID = 'gs-plan-range';

export const FILL_MODE_INPUT_ID = 'gs-plan-fill-mode';
export const PAIR_AUDITS_INPUT_ID = 'gs-plan-pair-audits';

/** The input each refusal field the server names belongs to; knob fields are resolved per knob. */
export const REFUSAL_INPUTS: Readonly<Record<string, string>> = {
  'policy.min_trades': numberInputId('min_trades'),
  expected_trades_per_year: numberInputId('expected_trades_per_year'),
  'policy.max_drawdown_ceiling': numberInputId('drawdown_percent'),
  exam_min_trades: numberInputId('exam_min_trades'),
  budget_cap: numberInputId('budget_cap'),
  training_months: numberInputId('training_months'),
  test_months: numberInputId('test_months'),
  'zoom.points': numberInputId('zoom_points'),
  'zoom.refinements': numberInputId('zoom_refinements'),
  'zoom.passes': numberInputId('zoom_passes'),
  'execution.initial_cash': numberInputId('initial_cash'),
  'execution.commission_per_order': numberInputId('commission_per_order'),
  'execution.slippage_per_share': numberInputId('slippage_per_share'),
  'execution.fill_mode': FILL_MODE_INPUT_ID,
  development_start_ms: RANGE_INPUT_ID,
  development_end_ms: RANGE_INPUT_ID,
  final_start_ms: RANGE_INPUT_ID,
  final_end_ms: RANGE_INPUT_ID,
  pair_audits: PAIR_AUDITS_INPUT_ID,
};

/** Which part of a knob's row a refusal names: its value cell, its step or its importance. */
export type KnobRefusalPart = 'value' | 'step' | 'importance';

/** The knob a refusal names (`knobs.<name>`, `knobs.<name>.step`, `knobs.<name>.importance` or `seed.<name>`), and the part. */
export function refusalKnob(field: string | null): { readonly name: string; readonly part: KnobRefusalPart } | null {
  const match = field === null ? null : /^(knobs|seed)\.(\w+)(?:\.(step|importance))?$/.exec(field);
  if (match === null || match[2] === 'symbol') return null;
  return { name: match[2], part: (match[3] as KnobRefusalPart | undefined) ?? 'value' };
}

/** The input a refusal's field belongs to in the form as it stands, or null. */
export function refusalTarget(field: string | null, knobs: readonly KnobPlan[]): string | null {
  if (field === null) return null;
  const named = refusalKnob(field);
  if (named === null) return REFUSAL_INPUTS[field] ?? null;
  const knob = knobs.find((plan) => plan.name === named.name);
  if (knob === undefined) return null;
  if (named.part === 'importance') return importanceInputId(knob.name);
  if (named.part === 'step') return knob.mode === 'search' ? knobInputId(knob.name, 'step') : null;
  return knobInputId(knob.name, knob.mode === 'search' ? 'low' : 'fixed_value');
}

/** A refusal message without its knob's label in front: the knob's row already names it. */
export function withoutLabel(message: string, label: string): string {
  return message.startsWith(`${label}: `) ? message.slice(label.length + 2) : message;
}

/** The values the form cannot read, in the order they were met, each named after its input. */
export function unreadableProblems(problems: ReadonlyMap<string, string>, capability: StrategyCapability | null): PlanProblem[] {
  const labels = new Map((capability?.knobs ?? []).map((knob) => [knob.name, knob.label]));
  return [...problems].map(([key, message]) => {
    const named = problemField(key);
    switch (named?.kind) {
      case 'knob':
        return { key, text: `${labels.get(named.name) ?? named.name} ${KNOB_FIELD_LABELS[named.field]}: ${message}`, target: knobInputId(named.name, named.field), ...UNREFUSED };
      case 'number':
        return { key, text: `${NUMBER_FIELD_LABELS[named.field]}: ${message}`, target: numberInputId(named.field), ...UNREFUSED };
      case 'range':
        return { key, text: `Study range: ${message}`, target: RANGE_INPUT_ID, ...UNREFUSED };
      default:
        return { key, text: message, target: null, ...UNREFUSED };
    }
  });
}

/**
 * The server's refusals, each pointing at its input when one owns it.
 *
 * A refusal that names a backfill offers its days only once `backfilled`
 * says that backfill was tried: the backfill is the remedy the refusal
 * names, and another range is what is left when it did not fetch the sessions.
 */
export function refusalProblems(
  refusals: readonly ProtocolRefusal[],
  knobs: readonly KnobPlan[],
  backfilled: (remedy: BackfillRemedy) => boolean,
): PlanProblem[] {
  return refusals.map((refusal) => {
    // A data plane older than this page names no backfill.
    const backfill = refusal.backfill ?? null;
    return {
      key: `${refusal.code}:${refusal.field ?? ''}:${refusal.message}`,
      text: refusal.message,
      target: refusalTarget(refusal.field, knobs),
      offers: backfill === null || backfilled(backfill) ? offeredDays(refusal) : [],
      backfill,
    };
  });
}

/**
 * The days a study range could start or end on instead of the one the server
 * refused. A later development start is a later first day, because
 * development starts where the range does; an earlier final-test end is an
 * earlier last day, because the final test ends where the range does. A
 * refusal of another date names nothing a range can take.
 */
function offeredDays(refusal: ProtocolRefusal): RangeOffer[] {
  // A data plane older than this page names no dates.
  const named = refusal.nearest_valid_ms ?? [];
  if (refusal.field === 'development_start_ms') return named.map((ms) => ({ edge: 'start', day: etIsoDate(ms) }));
  // The final test's end is the midnight after its last day.
  if (refusal.field === 'final_end_ms') return named.map((ms) => ({ edge: 'end', day: etIsoDate(ms - 1) }));
  return [];
}
