import type { ExpiryKind, OptionRight, StrikeKind } from './options-backtest.types';
import type { LegDraft, SetupDraft } from './setup-draft';
import { wholeNumber } from './typed-text';

/**
 * What a setup may hold, mirrored from the setup schema (`setup.py`). The
 * server still judges every body; these are here because a leg's contract
 * cannot be edited once the leg is added, so the builder says what is out of
 * range before the leg exists rather than after.
 */
export const MAX_LEGS = 8;
const MAX_CONTRACTS = 1000;
const MAX_EXPIRY_DAYS = 730;
const MAX_STRIKE_STEPS = 50;

/** The leg builder's row, as typed. */
export interface LegBuilder {
  right: OptionRight;
  contracts: string;
  expiryKind: ExpiryKind;
  expiryDays: string;
  strikeKind: StrikeKind;
  strikeSteps: string;
}

export type BuilderField = 'contracts' | 'expiryDays' | 'strikeSteps';

export function newLegBuilder(): LegBuilder {
  return { right: 'call', contracts: '1', expiryKind: 'this_week', expiryDays: '1', strikeKind: 'atm', strikeSteps: '1' };
}

/** Why the row cannot be added as a leg, by field; empty when it can. */
export function builderProblems(builder: LegBuilder): ReadonlyMap<BuilderField, string> {
  const problems = new Map<BuilderField, string>();
  if (wholeNumber(builder.contracts, 1, MAX_CONTRACTS) === null) {
    problems.set('contracts', `Enter a whole number of contracts, 1 to ${MAX_CONTRACTS}.`);
  }
  if (builder.expiryKind === 'min_days' && wholeNumber(builder.expiryDays, 0, MAX_EXPIRY_DAYS) === null) {
    problems.set('expiryDays', `Enter a whole number of days, 0 to ${MAX_EXPIRY_DAYS}.`);
  }
  if (builder.strikeKind === 'atm_offset') {
    const steps = wholeNumber(builder.strikeSteps, -MAX_STRIKE_STEPS, MAX_STRIKE_STEPS);
    if (steps === null || steps === 0) {
      problems.set('strikeSteps', `Enter a whole number of steps, -${MAX_STRIKE_STEPS} to ${MAX_STRIKE_STEPS}, other than 0. For none, choose At the money.`);
    }
  }
  return problems;
}

/** The leg a row makes, with every rule off. Call only when `builderProblems` is empty. */
export function legFromBuilder(builder: LegBuilder, takenIds: readonly string[]): LegDraft {
  return {
    id: nextLegId(builder.right, takenIds),
    right: builder.right,
    contracts: wholeNumber(builder.contracts, 1, MAX_CONTRACTS) ?? 1,
    expiryKind: builder.expiryKind,
    expiryDays: builder.expiryKind === 'min_days' ? (wholeNumber(builder.expiryDays, 0, MAX_EXPIRY_DAYS) ?? 0) : 0,
    strikeKind: builder.strikeKind,
    strikeSteps: builder.strikeKind === 'atm_offset' ? (wholeNumber(builder.strikeSteps, -MAX_STRIKE_STEPS, MAX_STRIKE_STEPS) ?? 0) : 0,
    stopLossOn: false,
    stopLossKind: 'percent',
    stopLossValue: '',
    momentumOn: false,
    momentumKind: 'percent_up',
    momentumValue: '',
    reentryOn: false,
    reentryKind: 'at_once',
    reentryCount: '1',
  };
}

/** A copy of a leg under an id of its own, rules and all. */
export function copyOfLeg(leg: LegDraft, takenIds: readonly string[]): LegDraft {
  return { ...leg, id: nextLegId(leg.right, takenIds) };
}

/**
 * An id the owner never types. It is the leg's right (`call`, `put`), then
 * `call_2`, `call_3`, so a straddle built here has the ids the shipped
 * example has and the plain-words summary reads "Leg call". It is a valid
 * leg id by construction (lower-case letters, digits, underscores, at most 32).
 */
export function nextLegId(right: OptionRight, takenIds: readonly string[]): string {
  if (!takenIds.includes(right)) return right;
  let count = 2;
  while (takenIds.includes(`${right}_${count}`)) count += 1;
  return `${right}_${count}`;
}

/** The leg's contract in one line, from the choices made in the builder. */
export function contractLine(leg: LegDraft): string {
  const right = leg.right === 'call' ? 'Call' : 'Put';
  return ['Buy', plural(leg.contracts, 'contract'), right, expiryText(leg), strikeText(leg)].join(' · ');
}

function expiryText(leg: LegDraft): string {
  if (leg.expiryKind === 'same_day') return 'Expires the same day';
  if (leg.expiryKind === 'this_week') return 'Expires this week';
  return `Expires at least ${plural(leg.expiryDays, 'day')} out`;
}

function strikeText(leg: LegDraft): string {
  if (leg.strikeKind === 'atm') return 'At the money';
  return `${plural(Math.abs(leg.strikeSteps), 'strike')} ${leg.strikeSteps > 0 ? 'above' : 'below'} the money`;
}

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? '' : 's'}`;
}

/** The setup with the builder's leg added; unchanged at the limit. Call only when `builderProblems` is empty. */
export function withLegAdded(draft: SetupDraft, builder: LegBuilder): SetupDraft {
  if (draft.legs.length >= MAX_LEGS) return draft;
  return { ...draft, legs: [...draft.legs, legFromBuilder(builder, draft.legs.map((leg) => leg.id))] };
}

/** The setup with a copy of the leg placed just after it; unchanged at the limit. */
export function withLegCopied(draft: SetupDraft, legId: string): SetupDraft {
  const at = draft.legs.findIndex((leg) => leg.id === legId);
  if (at < 0 || draft.legs.length >= MAX_LEGS) return draft;
  const copy = copyOfLeg(draft.legs[at], draft.legs.map((leg) => leg.id));
  return { ...draft, legs: [...draft.legs.slice(0, at + 1), copy, ...draft.legs.slice(at + 1)] };
}

export function withLegRemoved(draft: SetupDraft, legId: string): SetupDraft {
  return { ...draft, legs: draft.legs.filter((leg) => leg.id !== legId) };
}
