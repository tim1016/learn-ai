import type { ExpiryKind, FeeKind, MomentumKind, OptionRight, ReentryKind, SquareOff, StopLossKind, StrikeKind } from './options-backtest.types';

export interface Choice<T extends string> {
  readonly value: T;
  readonly label: string;
}

/** Every either-or on the page, in the order the owner reads it. */
export const RIGHT_CHOICES: readonly Choice<OptionRight>[] = [
  { value: 'call', label: 'Call' },
  { value: 'put', label: 'Put' },
];

export const EXPIRY_CHOICES: readonly Choice<ExpiryKind>[] = [
  { value: 'same_day', label: 'Same day' },
  { value: 'this_week', label: 'This week' },
  { value: 'min_days', label: 'At least N days' },
];

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

export function labelOf<T extends string>(choices: readonly Choice<T>[], value: T): string {
  return choices.find((choice) => choice.value === value)?.label ?? value;
}
