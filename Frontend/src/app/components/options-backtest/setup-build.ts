import type { LegRequest, SessionClock, SessionTimeWire, SetupRequest } from './options-backtest.types';
import { FIELD, legKey, type FieldProblems } from './setup-problems';
import { heldAnchorFor, type HeldAnchor, type LegDraft, type SetupDraft } from './setup-draft';
import { storedTime, type TimeRole } from './session-time';
import { decimalText, wholeNumber } from './typed-text';

export type BuildResult =
  | { readonly ok: true; readonly body: SetupRequest }
  | { readonly ok: false; readonly problems: FieldProblems };

/**
 * The one place a request body is made from the draft. A rule that is
 * switched off sends nothing, and its fields are not read. Decimals leave as
 * the text the owner typed, trimmed; whatever cannot be read is a problem at
 * its field, and then there is no body to send.
 */
export function buildSetup(draft: SetupDraft, clock: SessionClock): BuildResult {
  const problems = new Map<string, string>();
  const fail = (key: string, message: string): null => {
    problems.set(key, message);
    return null;
  };
  const timeAt = (role: TimeRole, text: string, held: HeldAnchor | null, key: string): SessionTimeWire | null => {
    const stored = storedTime(role, text, clock, heldAnchorFor(held, text));
    return stored.ok ? stored.time : fail(key, stored.problem);
  };
  const decimal = (text: string, key: string, example: string): string | null =>
    decimalText(text) ?? fail(key, `Enter a number, such as ${example}.`);

  if (draft.underlying === '') fail(FIELD.underlying, 'Pick an underlying.');
  const entry = timeAt('entry', draft.entryClock, draft.held.entry, FIELD.entry);
  const exit = timeAt('exit', draft.exitClock, draft.held.exit, FIELD.exit);
  const noReentry = draft.noReentryOn ? timeAt('noReentry', draft.noReentryClock, draft.held.noReentry, FIELD.noReentry) : null;
  const slippage = decimal(draft.slippage, FIELD.slippage, '0.3');
  const cash = decimal(draft.cash, FIELD.cash, '10000');
  const fee = draft.feeKind === 'per_contract' ? decimal(draft.perContractFee, FIELD.perContractFee, '0.65') : null;
  const legs = draft.legs.map((leg) => buildLeg(leg, problems));

  if (problems.size > 0 || entry === null || exit === null || slippage === null || cash === null) return { ok: false, problems };
  return {
    ok: true,
    body: {
      version: 1,
      underlying: draft.underlying,
      entry,
      exit,
      ...(noReentry === null ? {} : { no_reentry_after: noReentry }),
      square_off: draft.squareOff,
      legs: legs.flatMap((leg) => (leg === null ? [] : [leg])),
      slippage_percent: slippage,
      price_step_usd: draft.priceStep,
      fees: fee === null ? { kind: draft.feeKind } : { kind: 'per_contract', per_contract_usd: fee },
      starting_cash_usd: cash,
    },
  };
}

function buildLeg(leg: LegDraft, problems: Map<string, string>): LegRequest | null {
  const start = problems.size;
  const note = (field: Parameters<typeof legKey>[1], message: string): null => {
    problems.set(legKey(leg.id, field), message);
    return null;
  };
  const value = (text: string, field: 'stopLossValue' | 'momentumValue'): string | null =>
    decimalText(text) ?? note(field, 'Enter a number, such as 25.');

  const stopLoss = leg.stopLossOn ? value(leg.stopLossValue, 'stopLossValue') : null;
  const momentum = leg.momentumOn ? value(leg.momentumValue, 'momentumValue') : null;
  const reentryCount = leg.reentryOn ? wholeNumber(leg.reentryCount, 0, Number.MAX_SAFE_INTEGER) : null;
  if (leg.reentryOn && reentryCount === null) note('reentryCount', 'Enter a whole number of re-entries.');
  if (leg.reentryOn && leg.reentryKind === 'on_momentum' && !leg.momentumOn) {
    note('reentry', 'Re-entry on momentum needs Simple momentum switched on.');
  }
  if (problems.size > start) return null;

  return {
    leg_id: leg.id,
    right: leg.right,
    contracts: leg.contracts,
    expiry: leg.expiryKind === 'min_days' ? { kind: 'min_days', days: leg.expiryDays } : { kind: leg.expiryKind },
    strike: leg.strikeKind === 'atm_offset' ? { kind: 'atm_offset', steps: leg.strikeSteps } : { kind: 'atm' },
    ...(stopLoss === null ? {} : { stop_loss: { kind: leg.stopLossKind, value: stopLoss } }),
    ...(momentum === null ? {} : { momentum: { kind: leg.momentumKind, value: momentum } }),
    ...(reentryCount === null ? {} : { reentry_on_stop: { kind: leg.reentryKind, count: reentryCount } }),
  };
}
