/**
 * The Plan step's draft (#2696): a ProtocolRequest being edited plus the
 * inputs that cannot be read yet. Every control emits a `PlanEdit`;
 * `applyPlanEdit` turns it into the next draft without judging the plan —
 * whether the plan is acceptable is the server preflight's answer.
 */

import { etDayEndMs, etMidnightMs } from '../../shared/date/et-midnight';
import type { FillModeName } from '../../models/fill-mode';
import type { GoldenSearchMethod, KnobMode, KnobPair, KnobPlan, ProtocolRequest, RankingMeasure, StrategyCapability } from './golden-search.types';

export type KnobNumberField = 'low' | 'high' | 'fixed_value' | 'step';

export type ProtocolNumberField =
  | 'min_trades'
  | 'drawdown_percent'
  | 'commission_per_order'
  | 'slippage_per_share'
  | 'initial_cash'
  | 'training_months'
  | 'test_months'
  | 'zoom_points'
  | 'zoom_refinements'
  | 'zoom_passes'
  | 'exam_min_trades'
  | 'budget_cap';

/** `final_start` is also the development end: the two intervals share one boundary. `final_end` is the inclusive last day. */
export type ProtocolDateField = 'development_start' | 'final_start' | 'final_end';
export type ProtocolFlag = 'require_positive_net' | 'recent_window' | 'neighbor_audit';

export type PlanEdit =
  | { readonly kind: 'method'; readonly method: GoldenSearchMethod }
  | { readonly kind: 'knob-mode'; readonly name: string; readonly mode: KnobMode }
  | { readonly kind: 'knob-number'; readonly name: string; readonly field: KnobNumberField; readonly raw: string }
  | { readonly kind: 'knob-move'; readonly name: string; readonly offset: -1 | 1 }
  | { readonly kind: 'objective'; readonly objective: RankingMeasure }
  | { readonly kind: 'fill-mode'; readonly fillMode: FillModeName }
  | { readonly kind: 'number'; readonly field: ProtocolNumberField; readonly raw: string }
  | { readonly kind: 'date'; readonly field: ProtocolDateField; readonly raw: string }
  | { readonly kind: 'flag'; readonly field: ProtocolFlag; readonly value: boolean }
  | { readonly kind: 'pair'; readonly pair: KnobPair; readonly included: boolean };

export interface PlanDraft {
  readonly protocol: ProtocolRequest;
  /** Inputs that could not be read, keyed by `problemKey`; while any exist the plan is not preflighted. */
  readonly problems: ReadonlyMap<string, string>;
}

export function knobProblemKey(name: string, field: KnobNumberField): string {
  return `knob:${name}:${field}`;
}

export function numberProblemKey(field: ProtocolNumberField): string {
  return `number:${field}`;
}

export function dateProblemKey(field: ProtocolDateField): string {
  return `date:${field}`;
}

/** A number input's text as a finite number, or null for blank or unreadable text (never a silent 0). */
export function readNumber(raw: string): number | null {
  if (raw.trim() === '') return null;
  const value = Number(raw);
  return Number.isFinite(value) ? value : null;
}

function withProblem(problems: ReadonlyMap<string, string>, key: string, message: string | null): ReadonlyMap<string, string> {
  const next = new Map(problems);
  if (message === null) next.delete(key);
  else next.set(key, message);
  return next;
}

function samePair(a: KnobPair, b: KnobPair): boolean {
  return a[0] === b[0] && a[1] === b[1];
}

function patchKnob(protocol: ProtocolRequest, name: string, patch: (knob: KnobPlan) => KnobPlan): ProtocolRequest {
  return { ...protocol, knobs: protocol.knobs.map((knob) => (knob.name === name ? patch(knob) : knob)) };
}

/** Every searched knob needs its smallest step (under Zoom and Grid alike); the declaration's default step is the starting suggestion. */
function withSteps(protocol: ProtocolRequest, capability: StrategyCapability | null): ProtocolRequest {
  const steps = new Map((capability?.knobs ?? []).map((knob) => [knob.name, knob.default_step]));
  return {
    ...protocol,
    knobs: protocol.knobs.map((knob) => (knob.mode === 'search' && knob.step === null ? { ...knob, step: steps.get(knob.name) ?? null } : knob)),
  };
}

function moveKnob(protocol: ProtocolRequest, name: string, offset: -1 | 1): ProtocolRequest {
  const index = protocol.knobs.findIndex((knob) => knob.name === name);
  const target = index + offset;
  if (index < 0 || target < 0 || target >= protocol.knobs.length) return protocol;
  const knobs = [...protocol.knobs];
  [knobs[index], knobs[target]] = [knobs[target], knobs[index]];
  return { ...protocol, knobs };
}

function setNumber(protocol: ProtocolRequest, field: ProtocolNumberField, value: number): ProtocolRequest {
  switch (field) {
    case 'min_trades':
      return { ...protocol, policy: { ...protocol.policy, min_trades: value } };
    case 'drawdown_percent':
      return { ...protocol, policy: { ...protocol.policy, max_drawdown_ceiling: value / 100 } };
    case 'commission_per_order':
    case 'slippage_per_share':
    case 'initial_cash':
      return { ...protocol, execution: { ...protocol.execution, [field]: value } };
    case 'zoom_points':
      return { ...protocol, zoom: { ...protocol.zoom, points: value } };
    case 'zoom_refinements':
      return { ...protocol, zoom: { ...protocol.zoom, refinements: value } };
    case 'zoom_passes':
      return { ...protocol, zoom: { ...protocol.zoom, passes: value } };
    case 'training_months':
    case 'test_months':
    case 'exam_min_trades':
    case 'budget_cap':
      return { ...protocol, [field]: value };
  }
}

function setDate(protocol: ProtocolRequest, field: ProtocolDateField, isoDate: string): ProtocolRequest {
  switch (field) {
    case 'development_start':
      return { ...protocol, development_start_ms: etMidnightMs(isoDate) };
    case 'final_start': {
      const boundary = etMidnightMs(isoDate);
      return { ...protocol, development_end_ms: boundary, final_start_ms: boundary };
    }
    case 'final_end':
      return { ...protocol, final_end_ms: etDayEndMs(isoDate) };
  }
}

/** The draft after one edit. Unreadable input leaves the protocol as it was and records a problem instead. */
export function applyPlanEdit(draft: PlanDraft, edit: PlanEdit, capability: StrategyCapability | null): PlanDraft {
  const { protocol, problems } = draft;
  switch (edit.kind) {
    case 'method':
      return { protocol: { ...protocol, method: edit.method }, problems };
    case 'knob-mode':
      return { protocol: withSteps(patchKnob(protocol, edit.name, (knob) => ({ ...knob, mode: edit.mode })), capability), problems };
    case 'knob-number': {
      const key = knobProblemKey(edit.name, edit.field);
      const value = readNumber(edit.raw);
      if (value === null) return { protocol, problems: withProblem(problems, key, 'Enter a number.') };
      return { protocol: patchKnob(protocol, edit.name, (knob) => ({ ...knob, [edit.field]: value })), problems: withProblem(problems, key, null) };
    }
    case 'knob-move':
      return { protocol: moveKnob(protocol, edit.name, edit.offset), problems };
    case 'objective':
      return { protocol: { ...protocol, policy: { ...protocol.policy, objective: edit.objective } }, problems };
    case 'fill-mode':
      return { protocol: { ...protocol, execution: { ...protocol.execution, fill_mode: edit.fillMode } }, problems };
    case 'number': {
      const key = numberProblemKey(edit.field);
      const value = readNumber(edit.raw);
      if (value === null) return { protocol, problems: withProblem(problems, key, 'Enter a number.') };
      return { protocol: setNumber(protocol, edit.field, value), problems: withProblem(problems, key, null) };
    }
    case 'date': {
      const key = dateProblemKey(edit.field);
      if (!/^\d{4}-\d{2}-\d{2}$/.test(edit.raw)) return { protocol, problems: withProblem(problems, key, 'Enter a date as YYYY-MM-DD.') };
      return { protocol: setDate(protocol, edit.field, edit.raw), problems: withProblem(problems, key, null) };
    }
    case 'flag':
      if (edit.field === 'require_positive_net') return { protocol: { ...protocol, policy: { ...protocol.policy, require_positive_net: edit.value } }, problems };
      return { protocol: { ...protocol, [edit.field]: edit.value }, problems };
    case 'pair': {
      const kept = protocol.pair_audits.filter((pair) => !samePair(pair, edit.pair));
      return { protocol: { ...protocol, pair_audits: edit.included ? [...kept, edit.pair] : kept }, problems };
    }
  }
}
