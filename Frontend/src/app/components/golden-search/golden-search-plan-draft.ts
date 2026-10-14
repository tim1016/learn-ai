/**
 * The Plan step's draft (#2696): a ProtocolRequest being edited plus the
 * inputs that cannot be read yet. Every control emits a `PlanEdit`;
 * `applyPlanEdit` turns it into the next draft without judging the plan —
 * whether the plan is acceptable is the server preflight's answer.
 */

import { etDayEndMs, etMidnightMs } from '../../shared/date/et-midnight';
import type { FillModeName } from '../../models/fill-mode';
import { fractionFromPercent, usesTradeFrequency } from './golden-search-display';
import type { DefaultsLayout, GoldenSearchDefaults, GoldenSearchMethod, KnobMode, KnobPair, KnobPlan, ProtocolRequest, RankingMeasure, StrategyCapability } from './golden-search.types';

const KNOB_NUMBER_FIELDS = ['low', 'high', 'fixed_value', 'step'] as const;
export type KnobNumberField = (typeof KNOB_NUMBER_FIELDS)[number];

const PROTOCOL_NUMBER_FIELDS = [
  'final_months',
  'min_trades',
  'expected_trades_per_year',
  'drawdown_percent',
  'commission_per_order',
  'slippage_per_share',
  'initial_cash',
  'training_months',
  'test_months',
  'zoom_points',
  'zoom_refinements',
  'zoom_passes',
  'exam_min_trades',
  'budget_cap',
] as const;
export type ProtocolNumberField = (typeof PROTOCOL_NUMBER_FIELDS)[number];

/** Counts the protocol carries as integers; a fraction is unreadable, never rounded. */
const WHOLE_NUMBER_FIELDS: ReadonlySet<ProtocolNumberField> = new Set([
  'final_months',
  'min_trades',
  'expected_trades_per_year',
  'training_months',
  'test_months',
  'zoom_points',
  'zoom_refinements',
  'zoom_passes',
  'exam_min_trades',
  'budget_cap',
]);

/** The study range the trader picked: its first and last ET day (`YYYY-MM-DD`), both included. */
export interface StudyRange {
  readonly from: string;
  readonly to: string;
}
export type ProtocolFlag = 'require_positive_net' | 'recent_window' | 'neighbor_audit';

export type PlanEdit =
  | { readonly kind: 'method'; readonly method: GoldenSearchMethod }
  | { readonly kind: 'knob-mode'; readonly name: string; readonly mode: KnobMode }
  | { readonly kind: 'knob-number'; readonly name: string; readonly field: KnobNumberField; readonly raw: string }
  | { readonly kind: 'knob-importance'; readonly name: string; readonly importance: number }
  | { readonly kind: 'knobs-reset' }
  | { readonly kind: 'objective'; readonly objective: RankingMeasure }
  | { readonly kind: 'fill-mode'; readonly fillMode: FillModeName }
  | { readonly kind: 'number'; readonly field: ProtocolNumberField; readonly raw: string }
  /** The range picker's two days; an empty one means its field holds no usable date. */
  | { readonly kind: 'range'; readonly from: string; readonly to: string }
  | { readonly kind: 'flag'; readonly field: ProtocolFlag; readonly value: boolean }
  | { readonly kind: 'pair'; readonly pair: KnobPair; readonly included: boolean };

/** The knob table as the plan started: what "Reset to defaults" restores. */
export type KnobOrigin = Pick<ProtocolRequest, 'knobs' | 'seed' | 'pair_audits'>;

export interface PlanDraft {
  readonly protocol: ProtocolRequest;
  /** Inputs that could not be read, keyed by `problemKey`; while any exist the plan is not preflighted. */
  readonly problems: ReadonlyMap<string, string>;
  readonly origin: KnobOrigin;
  /**
   * The final test's length in months, from which the server lays the dates
   * out. Absent when the dates did not come from a month count (a revised
   * study's frozen dates): the protocol carries dates, not this count.
   */
  readonly finalMonths?: number;
  /**
   * Trailing sessions the server's layout left out of the final test because
   * the lake has not reached them yet. Absent with `finalMonths`.
   */
  readonly finalSessionsCut?: number;
  /**
   * The study range picked on the form, inside which the server lays the
   * dates out. Absent until one is picked: the plan keeps the dates it has.
   */
  readonly range?: StudyRange;
  /** Which sessions of the picked range the server's layout left out, and why; null when it used them all. */
  readonly rangeNote?: string | null;
  /**
   * The range the plan's dates were laid out inside. It is not `range` while a
   * newly picked range waits for its layout, nor after the server refused it:
   * the plan then still holds the dates of the range before.
   */
  readonly laidOutRange?: StudyRange;
}

/** A draft of `protocol` as it stands, with nothing unreadable; its knob table is what a reset restores. */
export function draftFromProtocol(protocol: ProtocolRequest): PlanDraft {
  return { protocol, problems: new Map(), origin: { knobs: protocol.knobs, seed: protocol.seed, pair_audits: protocol.pair_audits } };
}

/**
 * A fresh draft from `GET /defaults`, with the final test laid out as the
 * server laid it out. The incumbent's name and settings, the exposure, the
 * month count and the sessions cut describe the plan but are not part of it:
 * the server refuses them in a plan, so they never reach the protocol.
 */
export function draftFromDefaults(defaults: GoldenSearchDefaults): { readonly draft: PlanDraft; readonly incumbentLabel: string; readonly incumbentSentence: string } {
  const { incumbent_label, incumbent_sentence, exposure: _exposure, final_months, final_sessions_cut, range_note: _note, ...protocol } = defaults;
  return {
    draft: { ...draftFromProtocol(protocol), finalMonths: final_months, finalSessionsCut: final_sessions_cut },
    incumbentLabel: incumbent_label,
    incumbentSentence: incumbent_sentence,
  };
}

/** A searched knob whose range holds more than one value; a knob searched from a value to itself is held there. */
export function isVaried(knob: KnobPlan): boolean {
  return knob.mode === 'search' && knob.low !== knob.high;
}

/**
 * The plan as the server receives it. A knob searched from a value to itself
 * is sent held at that value (the server refuses an empty range, and holding
 * is what it means), and a pair audit is sent only while both its knobs vary
 * — the draft keeps the pair, so it returns when they do.
 */
export function wireProtocol(protocol: ProtocolRequest): ProtocolRequest {
  let wired: ProtocolRequest = protocol;
  for (const knob of protocol.knobs) {
    if (knob.mode !== 'search' || isVaried(knob)) continue;
    wired = withFixedSeed(patchKnob(wired, knob.name, (plan) => ({ ...plan, mode: 'fixed', fixed_value: plan.low })), knob.name);
  }
  const varied = new Set(wired.knobs.filter(isVaried).map((knob) => knob.name));
  const pairs = wired.pair_audits.filter(([left, right]) => varied.has(left) && varied.has(right));
  return pairs.length === wired.pair_audits.length ? wired : { ...wired, pair_audits: pairs };
}

/** Month counts that, once changed, have the server lay the development and final-test dates out again. */
export const MONTH_FIELDS: ReadonlySet<ProtocolNumberField> = new Set(['final_months', 'training_months', 'test_months']);

/** The problem key of a study range with a day the picker cannot use. */
export const RANGE_PROBLEM_KEY = 'range';

/** A revised plan's frozen dates carry no final-test length, and a range is laid out from one. */
const NEEDS_FINAL_MONTHS = "Enter the final test's length in months; the dates are laid out from it.";

/**
 * What to ask `/defaults` to lay the dates out from: the three month counts,
 * and the picked range once there is one. Null while a count is unknown or
 * unreadable, or the range holds a day the picker cannot use.
 */
export function draftLayout(draft: PlanDraft): DefaultsLayout | null {
  if (draft.finalMonths === undefined || draft.problems.has(RANGE_PROBLEM_KEY) || [...MONTH_FIELDS].some((field) => draft.problems.has(numberProblemKey(field)))) return null;
  const months = { final_months: draft.finalMonths, training_months: draft.protocol.training_months, test_months: draft.protocol.test_months };
  // The range's last day is included, so it ends at the ET midnight after it.
  return draft.range === undefined ? months : { ...months, range_start_ms: etMidnightMs(draft.range.from), range_end_ms: etDayEndMs(draft.range.to) };
}

/** The draft with the dates the server laid out, and what it left out of a picked range; every other edit, the counts and the range included, is kept. */
export function withServerDates(draft: PlanDraft, laidOut: GoldenSearchDefaults): PlanDraft {
  const { development_start_ms, development_end_ms, final_start_ms, final_end_ms, final_sessions_cut, range_note } = laidOut;
  return { ...draft, protocol: { ...draft.protocol, development_start_ms, development_end_ms, final_start_ms, final_end_ms }, finalSessionsCut: final_sessions_cut, rangeNote: range_note ?? null, laidOutRange: draft.range };
}

export function knobProblemKey(name: string, field: KnobNumberField): string {
  return `knob:${name}:${field}`;
}

export function numberProblemKey(field: ProtocolNumberField): string {
  return `number:${field}`;
}

/** The field a problem key names. */
export type ProblemField =
  | { readonly kind: 'knob'; readonly name: string; readonly field: KnobNumberField }
  | { readonly kind: 'number'; readonly field: ProtocolNumberField }
  | { readonly kind: 'range' };

/** The inverse of `knobProblemKey` and `numberProblemKey`, and the range's own key; null for a key they did not make. */
export function problemField(key: string): ProblemField | null {
  const [kind, first, second] = key.split(':');
  if (kind === 'knob' && first !== undefined && isOneOf(KNOB_NUMBER_FIELDS, second)) return { kind, name: first, field: second };
  if (kind === 'number' && isOneOf(PROTOCOL_NUMBER_FIELDS, first)) return { kind, field: first };
  if (key === RANGE_PROBLEM_KEY) return { kind: 'range' };
  return null;
}

function isOneOf<T extends string>(values: readonly T[], value: string | undefined): value is T {
  return values.some((candidate) => candidate === value);
}

/** The knob's value inputs the table shows: a held knob's value, a range's ends, and a step only when the ends differ. */
function shownKnobFields(knob: KnobPlan): readonly KnobNumberField[] {
  if (knob.mode === 'fixed') return ['fixed_value'];
  return isVaried(knob) ? ['low', 'high', 'step'] : ['low', 'high'];
}

/** Drops a knob's unreadable values whose inputs the table no longer shows, so no problem points at a hidden field. */
function withoutHiddenKnobProblems(problems: ReadonlyMap<string, string>, protocol: ProtocolRequest, name: string): ReadonlyMap<string, string> {
  const knob = protocol.knobs.find((candidate) => candidate.name === name);
  if (knob === undefined) return problems;
  const shown = shownKnobFields(knob);
  const hidden = KNOB_NUMBER_FIELDS.filter((field) => !shown.includes(field)).map((field) => knobProblemKey(name, field));
  if (!hidden.some((key) => problems.has(key))) return problems;
  return new Map([...problems].filter(([key]) => !hidden.includes(key)));
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

/**
 * A fixed knob holds one value, and the searches start from the seed, so the
 * two must agree: the server refuses a seed that contradicts a fixed knob
 * (`SEED_CONFLICTS_FIXED`), and the form has no separate seed control.
 */
function withFixedSeed(protocol: ProtocolRequest, name: string): ProtocolRequest {
  const knob = protocol.knobs.find((candidate) => candidate.name === name);
  if (knob === undefined || knob.mode !== 'fixed' || !protocol.seed) return protocol;
  return { ...protocol, seed: { ...protocol.seed, [name]: knob.fixed_value } };
}

/** Every searched knob needs its smallest step (under Zoom and Grid alike); the declaration's default step is the starting suggestion. */
function withSteps(protocol: ProtocolRequest, capability: StrategyCapability | null): ProtocolRequest {
  const steps = new Map((capability?.knobs ?? []).map((knob) => [knob.name, knob.default_step]));
  return {
    ...protocol,
    knobs: protocol.knobs.map((knob) => (knob.mode === 'search' && knob.step === null ? { ...knob, step: steps.get(knob.name) ?? null } : knob)),
  };
}

/** Whether every knob carries an importance score; a legacy plan has none and keeps its own order. */
export function isRanked(knobs: readonly KnobPlan[]): boolean {
  return knobs.every((knob) => knob.importance != null);
}

/**
 * Knobs in search order (ADR 0074 decision 10): higher importance first, ties
 * in the strategy's declared order. A plan where any knob has no importance (a
 * legacy plan) keeps its own order. The server's `protocol.by_importance`
 * freezes the same order; contracts/fixtures/golden-search-importance-order-v1.json pins both.
 */
export function byImportance(knobs: readonly KnobPlan[], capability: StrategyCapability | null): KnobPlan[] {
  // The server sorts only whole-number scores too; anything else is refused knob by knob.
  if (!knobs.every((knob) => Number.isInteger(knob.importance))) return [...knobs];
  const rank = new Map((capability?.knobs ?? []).map((knob, index) => [knob.name, index]));
  const position = (name: string): number => rank.get(name) ?? rank.size;
  return [...knobs].sort((a, b) => (b.importance ?? 0) - (a.importance ?? 0) || position(a.name) - position(b.name));
}

/** A plan with every knob scored (a legacy plan's at the scale's starting importance), in the order the server will freeze. */
export function withImportance(protocol: ProtocolRequest, capability: StrategyCapability | null): ProtocolRequest {
  const start = capability?.importance.default;
  if (start === undefined) return protocol;
  return { ...protocol, knobs: byImportance(protocol.knobs.map((knob) => ({ ...knob, importance: knob.importance ?? start })), capability) };
}

function setNumber(protocol: ProtocolRequest, field: Exclude<ProtocolNumberField, 'final_months'>, value: number): ProtocolRequest {
  switch (field) {
    case 'min_trades':
      return { ...protocol, policy: { ...protocol.policy, min_trades: value } };
    case 'drawdown_percent':
      return { ...protocol, policy: { ...protocol.policy, max_drawdown_ceiling: fractionFromPercent(value) } };
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
    case 'expected_trades_per_year':
      return { ...protocol, [field]: value };
  }
}

/** The draft after one edit. Unreadable input leaves the protocol as it was and records a problem instead. */
export function applyPlanEdit(draft: PlanDraft, edit: PlanEdit, capability: StrategyCapability | null): PlanDraft {
  const { protocol, problems } = draft;
  switch (edit.kind) {
    case 'method':
      return { ...draft, protocol: { ...protocol, method: edit.method }, problems };
    case 'knob-mode': {
      const moded = withFixedSeed(withSteps(patchKnob(protocol, edit.name, (knob) => ({ ...knob, mode: edit.mode })), capability), edit.name);
      return { ...draft, protocol: moded, problems: withoutHiddenKnobProblems(problems, moded, edit.name) };
    }
    case 'knob-number': {
      const key = knobProblemKey(edit.name, edit.field);
      const value = readNumber(edit.raw);
      if (value === null) return { ...draft, protocol, problems: withProblem(problems, key, 'Enter a number.') };
      const patched = withFixedSeed(patchKnob(protocol, edit.name, (knob) => ({ ...knob, [edit.field]: value })), edit.name);
      return { ...draft, protocol: patched, problems: withoutHiddenKnobProblems(withProblem(problems, key, null), patched, edit.name) };
    }
    case 'knob-importance': {
      const scored = patchKnob(protocol, edit.name, (knob) => ({ ...knob, importance: edit.importance }));
      return { ...draft, protocol: { ...scored, knobs: byImportance(scored.knobs, capability) }, problems };
    }
    case 'knobs-reset': {
      const { knobs, seed, pair_audits } = draft.origin;
      const kept = new Map([...problems].filter(([key]) => problemField(key)?.kind !== 'knob'));
      return { ...draft, protocol: { ...protocol, knobs, seed, pair_audits }, problems: kept };
    }
    case 'objective':
      return { ...draft, protocol: { ...protocol, policy: { ...protocol.policy, objective: edit.objective } }, problems };
    case 'fill-mode':
      return { ...draft, protocol: { ...protocol, execution: { ...protocol.execution, fill_mode: edit.fillMode } }, problems };
    case 'number': {
      const key = numberProblemKey(edit.field);
      const value = readNumber(edit.raw);
      if (value === null) return { ...draft, protocol, problems: withProblem(problems, key, 'Enter a number.') };
      if (WHOLE_NUMBER_FIELDS.has(edit.field) && !Number.isInteger(value)) return { ...draft, problems: withProblem(problems, key, 'Enter a whole number.') };
      if (edit.field === 'final_months') {
        // The cut belonged to the old count; the server's new dates bring their own.
        const { finalSessionsCut: _cut, ...rest } = draft;
        return { ...rest, finalMonths: value, problems: withProblem(problems, key, null) };
      }
      if (edit.field === 'expected_trades_per_year' && !usesTradeFrequency(protocol)) {
        // Adopting a frequency drops both fixed floors and their now-hidden problems (ADR 0074).
        const annual = { ...protocol, policy: { ...protocol.policy, min_trades: null }, exam_min_trades: null };
        const readable = withProblem(withProblem(withProblem(problems, key, null), numberProblemKey('min_trades'), null), numberProblemKey('exam_min_trades'), null);
        return { ...draft, protocol: setNumber(annual, edit.field, value), problems: readable };
      }
      return { ...draft, protocol: setNumber(protocol, edit.field, value), problems: withProblem(problems, key, null) };
    }
    case 'range': {
      // The picker says under itself why a day is unusable; the plan keeps its dates and waits.
      if (edit.from === '' || edit.to === '') return { ...draft, problems: withProblem(problems, RANGE_PROBLEM_KEY, 'Enter a first and a last day.') };
      // What the server said about the last range, and its cut, belong to the dates being replaced.
      const { finalSessionsCut: _cut, rangeNote: _note, ...rest } = draft;
      const ranged = withProblem(problems, RANGE_PROBLEM_KEY, null);
      const counted = draft.finalMonths === undefined ? withProblem(ranged, numberProblemKey('final_months'), NEEDS_FINAL_MONTHS) : ranged;
      return { ...rest, range: { from: edit.from, to: edit.to }, problems: counted };
    }
    case 'flag':
      if (edit.field === 'require_positive_net') return { ...draft, protocol: { ...protocol, policy: { ...protocol.policy, require_positive_net: edit.value } }, problems };
      return { ...draft, protocol: { ...protocol, [edit.field]: edit.value }, problems };
    case 'pair': {
      const kept = protocol.pair_audits.filter((pair) => !samePair(pair, edit.pair));
      return { ...draft, protocol: { ...protocol, pair_audits: edit.included ? [...kept, edit.pair] : kept }, problems };
    }
  }
}
