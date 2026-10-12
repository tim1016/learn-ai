import type {
  Anchor,
  ExpiryKind,
  FeeKind,
  MomentumKind,
  OptionRight,
  ReentryKind,
  SessionClock,
  SetupRequest,
  SquareOff,
  StopLossKind,
  StrikeKind,
} from './options-backtest.types';
import { anchorFor, clockTextOf, formatClockText, parseClockText } from './session-time';

/** What a setup starts as; the same numbers the server's own defaults carry. */
export const DEFAULT_SLIPPAGE = '0.3';
export const DEFAULT_PRICE_STEP = '0.01';
const DEFAULT_CASH = '10000';
const DEFAULT_ENTRY_MINUTES_AFTER_OPEN = 5;
const DEFAULT_EXIT_MINUTES_BEFORE_CLOSE = 15;
const DEFAULT_NO_REENTRY_MINUTES_BEFORE_CLOSE = 60;

/**
 * One leg as the page edits it. The contract (right, contracts, expiry,
 * strike) is fixed when the leg is added; the three rules are switched on and
 * off, and a rule that is off keeps its fields but sends nothing.
 */
export interface LegDraft {
  id: string;
  right: OptionRight;
  contracts: number;
  expiryKind: ExpiryKind;
  expiryDays: number;
  strikeKind: StrikeKind;
  strikeSteps: number;
  stopLossOn: boolean;
  stopLossKind: StopLossKind;
  stopLossValue: string;
  momentumOn: boolean;
  momentumKind: MomentumKind;
  momentumValue: string;
  reentryOn: boolean;
  reentryKind: ReentryKind;
  reentryCount: string;
}

/** An anchor a loaded setup came with that the page's own rule would not choose, kept while the clock text is as loaded. */
export interface HeldAnchor {
  readonly clock: string;
  readonly anchor: Anchor;
}

export interface HeldAnchors {
  entry: HeldAnchor | null;
  exit: HeldAnchor | null;
  noReentry: HeldAnchor | null;
}

/**
 * The setup as the owner edits it: text for everything typed (a time as
 * `HH:MM`, a decimal as the digits typed), booleans for switches. Nothing in
 * it is a number the page computes with; `buildSetup` makes the request body.
 */
export interface SetupDraft {
  underlying: string;
  entryClock: string;
  exitClock: string;
  noReentryOn: boolean;
  noReentryClock: string;
  held: HeldAnchors;
  squareOff: SquareOff;
  legs: LegDraft[];
  slippage: string;
  priceStep: string;
  feeKind: FeeKind;
  perContractFee: string;
  cash: string;
}

const NO_HELD: HeldAnchors = { entry: null, exit: null, noReentry: null };

/** A new setup. Until the clock is read the three times are empty; `withDefaultTimes` fills them once it is. */
export function newSetupDraft(clock: SessionClock | null): SetupDraft {
  const draft: SetupDraft = {
    underlying: 'SPY',
    entryClock: '',
    exitClock: '',
    noReentryOn: false,
    noReentryClock: '',
    held: NO_HELD,
    squareOff: 'partial',
    legs: [],
    slippage: DEFAULT_SLIPPAGE,
    priceStep: DEFAULT_PRICE_STEP,
    feeKind: 'not_charged',
    perContractFee: '',
    cash: DEFAULT_CASH,
  };
  return clock === null ? draft : withDefaultTimes(draft, clock);
}

/** The times still empty, set from the clock: five minutes in, fifteen before the close, a re-entry cutoff an hour before it. */
export function withDefaultTimes(draft: SetupDraft, clock: SessionClock): SetupDraft {
  return {
    ...draft,
    entryClock: draft.entryClock || formatClockText(clock.open_minute_et + DEFAULT_ENTRY_MINUTES_AFTER_OPEN),
    exitClock: draft.exitClock || formatClockText(clock.close_minute_et - DEFAULT_EXIT_MINUTES_BEFORE_CLOSE),
    noReentryClock: draft.noReentryClock || formatClockText(clock.close_minute_et - DEFAULT_NO_REENTRY_MINUTES_BEFORE_CLOSE),
  };
}

/** A served or imported setup as a draft, its times turned back into clock times with the served clock. */
export function draftFromSetup(setup: SetupRequest, clock: SessionClock): SetupDraft {
  const base = newSetupDraft(clock);
  const entryClock = clockTextOf(setup.entry, clock);
  const exitClock = clockTextOf(setup.exit, clock);
  const noReentry = setup.no_reentry_after ?? null;
  const noReentryClock = noReentry === null ? base.noReentryClock : clockTextOf(noReentry, clock);
  return {
    ...base,
    underlying: setup.underlying,
    entryClock,
    exitClock,
    noReentryOn: noReentry !== null,
    noReentryClock,
    held: {
      entry: heldAnchor('entry', entryClock, setup.entry.anchor),
      exit: heldAnchor('exit', exitClock, setup.exit.anchor),
      noReentry: noReentry === null ? null : heldAnchor('noReentry', noReentryClock, noReentry.anchor),
    },
    squareOff: setup.square_off,
    legs: setup.legs.map(legFromSetup),
    slippage: String(setup.slippage_percent ?? DEFAULT_SLIPPAGE),
    priceStep: String(setup.price_step_usd ?? DEFAULT_PRICE_STEP),
    feeKind: setup.fees.kind,
    perContractFee: setup.fees.per_contract_usd == null ? '' : String(setup.fees.per_contract_usd),
    cash: String(setup.starting_cash_usd),
  };
}

function heldAnchor(role: 'entry' | 'exit' | 'noReentry', clock: string, anchor: Anchor): HeldAnchor | null {
  const minute = parseClockText(clock);
  if (minute === null) return null;
  return anchorFor(role, minute) === anchor ? null : { clock, anchor };
}

function legFromSetup(leg: SetupRequest['legs'][number]): LegDraft {
  return {
    id: leg.leg_id,
    right: leg.right,
    contracts: leg.contracts,
    expiryKind: leg.expiry.kind,
    expiryDays: leg.expiry.kind === 'min_days' ? leg.expiry.days : 0,
    strikeKind: leg.strike.kind,
    strikeSteps: leg.strike.kind === 'atm_offset' ? leg.strike.steps : 0,
    stopLossOn: leg.stop_loss != null,
    stopLossKind: leg.stop_loss?.kind ?? 'percent',
    stopLossValue: leg.stop_loss == null ? '' : String(leg.stop_loss.value),
    momentumOn: leg.momentum != null,
    momentumKind: leg.momentum?.kind ?? 'percent_up',
    momentumValue: leg.momentum == null ? '' : String(leg.momentum.value),
    reentryOn: leg.reentry_on_stop != null,
    reentryKind: leg.reentry_on_stop?.kind ?? 'at_once',
    reentryCount: leg.reentry_on_stop == null ? '1' : String(leg.reentry_on_stop.count),
  };
}

/** The anchor to keep for a typed time: the one loaded with the setup, while the text is still as loaded. */
export function heldAnchorFor(held: HeldAnchor | null, text: string): Anchor | undefined {
  return held !== null && held.clock === text ? held.anchor : undefined;
}
