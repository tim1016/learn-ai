import type { ExpiryKind, FeeKind, LegSide, MomentumKind, OptionRight, ReentryKind, SquareOff, StopLossKind, StrikeKind } from './options-backtest.types';

export interface Choice<T extends string> {
  readonly value: T;
  readonly label: string;
}

/** Every either-or on the page, in the order the owner reads it. */
export const SIDE_CHOICES: readonly Choice<LegSide>[] = [
  { value: 'buy', label: 'Buy' },
  { value: 'sell', label: 'Sell' },
];

export const RIGHT_CHOICES: readonly Choice<OptionRight>[] = [
  { value: 'call', label: 'Call' },
  { value: 'put', label: 'Put' },
];

export const EXPIRY_CHOICES: readonly Choice<ExpiryKind>[] = [
  { value: 'same_day', label: 'Same day' },
  { value: 'this_week', label: 'Weekly' },
  { value: 'min_days', label: 'At least N days' },
];

/**
 * What each expiry choice picks, in a line under the leg builder's row while
 * it is the chosen one. The wording follows the schema's own
 * (`setup.py`), whose rule names do not say it: "this week" is not the current
 * week for an underlying that lists no expiry on its last trading day.
 */
export const EXPIRY_HINTS: Readonly<Record<ExpiryKind, string>> = {
  same_day: 'Same day: the expiry on the trading date itself.',
  this_week:
    'Weekly: the nearest listed expiry, today included, that falls on the last trading day of its week. For an underlying with none this week, that is one in a later week.',
  min_days: 'At least N days: the nearest listed expiry at least N calendar days after the trading date.',
};

export const STRIKE_CHOICES: readonly Choice<StrikeKind>[] = [
  { value: 'atm', label: 'At the money' },
  { value: 'atm_offset', label: 'Steps from the money' },
];

export const SQUARE_OFF_CHOICES: readonly Choice<SquareOff>[] = [
  { value: 'partial', label: 'Partial' },
  { value: 'complete', label: 'Complete' },
];

export const STOP_LOSS_CHOICES: readonly Choice<StopLossKind>[] = [
  { value: 'percent', label: 'Percent' },
  { value: 'points', label: 'Points' },
];

export const MOMENTUM_CHOICES: readonly Choice<MomentumKind>[] = [
  { value: 'percent_up', label: 'Percent up' },
  { value: 'percent_down', label: 'Percent down' },
  { value: 'points_up', label: 'Points up' },
  { value: 'points_down', label: 'Points down' },
];

export const REENTRY_CHOICES: readonly Choice<ReentryKind>[] = [
  { value: 'at_once', label: 'At once' },
  { value: 'on_momentum', label: 'On momentum' },
];

export const FEE_CHOICES: readonly Choice<FeeKind>[] = [
  { value: 'not_charged', label: 'Not charged' },
  { value: 'per_contract', label: 'Per contract' },
  { value: 'estimated', label: 'Estimated' },
];
