/**
 * The Plan step's problems in plain words (#2696), each pointing at the input
 * it belongs to: values the form cannot read yet, then the refusals the
 * server preflight returned. Every plan input carries the id these helpers
 * name, so the plan footer can take the trader straight to the field.
 */

import { problemField, type KnobNumberField, type ProtocolDateField, type ProtocolNumberField } from './golden-search-plan-draft';
import type { KnobPlan, ProtocolRefusal, StrategyCapability } from './golden-search.types';

export interface PlanProblem {
  readonly key: string;
  readonly text: string;
  /** The id of the input to fix, or null when no single input owns the problem. */
  readonly target: string | null;
}

export const NUMBER_FIELD_LABELS: Readonly<Record<ProtocolNumberField, string>> = {
  min_trades: 'Min completed trades',
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

export const DATE_FIELD_LABELS: Readonly<Record<ProtocolDateField, string>> = {
  development_start: 'Development from',
  final_start: 'Final test from',
  final_end: 'Final test through',
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

export function numberInputId(field: ProtocolNumberField): string {
  return `gs-plan-${field}`;
}

export function dateInputId(field: ProtocolDateField): string {
  return `gs-plan-date-${field}`;
}

export const FILL_MODE_INPUT_ID = 'gs-plan-fill-mode';
export const PAIR_AUDITS_INPUT_ID = 'gs-plan-pair-audits';

/** The input each refusal field the server names belongs to; knob fields are resolved per knob. */
export const REFUSAL_INPUTS: Readonly<Record<string, string>> = {
  'policy.min_trades': numberInputId('min_trades'),
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
  development_start_ms: dateInputId('development_start'),
  development_end_ms: dateInputId('final_start'),
  final_start_ms: dateInputId('final_start'),
  final_end_ms: dateInputId('final_end'),
  pair_audits: PAIR_AUDITS_INPUT_ID,
};

/** The knob a refusal names (`knobs.<name>`, `knobs.<name>.step` or `seed.<name>`), and whether it names the step. */
export function refusalKnob(field: string | null): { readonly name: string; readonly step: boolean } | null {
  const match = field === null ? null : /^(knobs|seed)\.(\w+)(\.step)?$/.exec(field);
  if (match === null || match[2] === 'symbol') return null;
  return { name: match[2], step: match[3] !== undefined };
}

/** The input a refusal's field belongs to in the form as it stands, or null. */
export function refusalTarget(field: string | null, knobs: readonly KnobPlan[]): string | null {
  if (field === null) return null;
  const named = refusalKnob(field);
  if (named === null) return REFUSAL_INPUTS[field] ?? null;
  const knob = knobs.find((plan) => plan.name === named.name);
  if (knob === undefined) return null;
  if (named.step) return knob.mode === 'search' ? knobInputId(knob.name, 'step') : null;
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
        return { key, text: `${labels.get(named.name) ?? named.name} ${KNOB_FIELD_LABELS[named.field]}: ${message}`, target: knobInputId(named.name, named.field) };
      case 'number':
        return { key, text: `${NUMBER_FIELD_LABELS[named.field]}: ${message}`, target: numberInputId(named.field) };
      case 'date':
        return { key, text: `${DATE_FIELD_LABELS[named.field]}: ${message}`, target: dateInputId(named.field) };
      default:
        return { key, text: message, target: null };
    }
  });
}

/** The server's refusals, each pointing at its input when one owns it. */
export function refusalProblems(refusals: readonly ProtocolRefusal[], knobs: readonly KnobPlan[]): PlanProblem[] {
  return refusals.map((refusal) => ({
    key: `${refusal.code}:${refusal.field ?? ''}:${refusal.message}`,
    text: refusal.message,
    target: refusalTarget(refusal.field, knobs),
  }));
}
