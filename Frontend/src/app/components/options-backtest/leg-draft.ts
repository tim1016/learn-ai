import type { ExpiryKind, OptionRight, SetupLimits, StrikeKind } from './options-backtest.types';
import type { LegDraft, SetupDraft } from './setup-draft';
import { legsFull } from './setup-limits';
import { wholeNumber } from './typed-text';

/** The largest whole number the page reads where the server's limit is not known; the server judges the rest. */
const NO_UPPER_LIMIT = Number.MAX_SAFE_INTEGER;

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

/**
 * Why the row cannot be added as a leg, by field; empty when it can. A leg's
 * contract cannot be edited once the leg is added, so the builder says what is
 * out of range before the leg exists. The ranges are the server's own
 * (`limits`); without them only the shape of a whole number is checked.
 */
export function builderProblems(builder: LegBuilder, limits: SetupLimits | null): ReadonlyMap<BuilderField, string> {
  const problems = new Map<BuilderField, string>();
  const maxContracts = limits?.max_contracts_per_leg;
  if (wholeNumber(builder.contracts, 1, maxContracts ?? NO_UPPER_LIMIT) === null) {
    problems.set('contracts', `Enter a whole number of contracts, ${maxContracts === undefined ? '1 or more' : `1 to ${maxContracts}`}.`);
  }
  const maxDays = limits?.max_expiry_days;
  if (builder.expiryKind === 'min_days' && wholeNumber(builder.expiryDays, 0, maxDays ?? NO_UPPER_LIMIT) === null) {
    problems.set('expiryDays', `Enter a whole number of days, ${maxDays === undefined ? '0 or more' : `0 to ${maxDays}`}.`);
  }
  if (builder.strikeKind === 'atm_offset') {
    const maxSteps = limits?.max_strike_steps;
    const steps = wholeNumber(builder.strikeSteps, maxSteps === undefined ? -NO_UPPER_LIMIT : -maxSteps, maxSteps ?? NO_UPPER_LIMIT);
    if (steps === null || steps === 0) {
      const range = maxSteps === undefined ? 'other than 0' : `-${maxSteps} to ${maxSteps}, other than 0`;
      problems.set('strikeSteps', `Enter a whole number of steps, ${range}. For none, choose At the money.`);
    }
  }
  return problems;
}

/** The leg a row makes, with every rule off. Call only when `builderProblems` is empty, which has checked every range. */
export function legFromBuilder(builder: LegBuilder, takenIds: readonly string[]): LegDraft {
  return {
    id: nextLegId(builder.right, takenIds),
    right: builder.right,
    contracts: wholeNumber(builder.contracts, 1, NO_UPPER_LIMIT) ?? 1,
    expiryKind: builder.expiryKind,
    expiryDays: builder.expiryKind === 'min_days' ? (wholeNumber(builder.expiryDays, 0, NO_UPPER_LIMIT) ?? 0) : 0,
    strikeKind: builder.strikeKind,
    strikeSteps: builder.strikeKind === 'atm_offset' ? (wholeNumber(builder.strikeSteps, -NO_UPPER_LIMIT, NO_UPPER_LIMIT) ?? 0) : 0,
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
  if (leg.expiryKind === 'this_week') return 'Weekly expiry';
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
export function withLegAdded(draft: SetupDraft, builder: LegBuilder, limits: SetupLimits | null): SetupDraft {
  if (legsFull(draft.legs.length, limits)) return draft;
  return { ...draft, legs: [...draft.legs, legFromBuilder(builder, draft.legs.map((leg) => leg.id))] };
}

/** The setup with a copy of the leg placed just after it; unchanged at the limit. */
export function withLegCopied(draft: SetupDraft, legId: string, limits: SetupLimits | null): SetupDraft {
  const at = draft.legs.findIndex((leg) => leg.id === legId);
  if (at < 0 || legsFull(draft.legs.length, limits)) return draft;
  const copy = copyOfLeg(draft.legs[at], draft.legs.map((leg) => leg.id));
  return { ...draft, legs: [...draft.legs.slice(0, at + 1), copy, ...draft.legs.slice(at + 1)] };
}

export function withLegRemoved(draft: SetupDraft, legId: string): SetupDraft {
  return { ...draft, legs: draft.legs.filter((leg) => leg.id !== legId) };
}
