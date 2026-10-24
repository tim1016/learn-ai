import type { SetupLimits } from '../../../../components/options-backtest/options-backtest.types';

/** What the owner has typed into the strikes box of the Past · Archive chain: digits, or nothing for the server's own number. */
export interface StrikesBox {
  strikes: string;
}

export const NO_STRIKES_TYPED: StrikesBox = { strikes: '' };

/** How long the strikes box waits after a keystroke before it counts as typed. Leaving the box or pressing Enter counts at once. */
export const STRIKES_PAUSE_MS = 400;

/** What the strikes box says: nothing (the server's own number), a count as the digits typed, or a refusal. */
export type ChosenStrikes =
  | { readonly kind: 'default' }
  | { readonly kind: 'count'; readonly digits: string }
  | { readonly kind: 'refused'; readonly problem: string };

/** The most strikes each side the server takes, as the digits it is written with; null while the server's limits are not known. */
export function mostStrikesText(limits: Pick<SetupLimits, 'max_strikes_each_side'> | null): string | null {
  const text = limits === null ? '' : String(limits.max_strikes_each_side);
  return /^\d+$/.test(text) ? text : null;
}

/** Whether a count written as digits with no leading zero is greater than another: by length, then digit by digit. No number is made. */
function isMoreThan(digits: string, most: string): boolean {
  return digits.length !== most.length ? digits.length > most.length : digits > most;
}

/**
 * The strikes are sent as the digits typed (no leading zeros), never as a number the page made. A count above the most
 * the server takes (its limit, when the page has it) is refused here, before anything is asked; with no limit known the
 * server judges it, and a count it refuses comes back as a refusal, shown at this box.
 */
export function chosenStrikes(text: string, most: string | null = null): ChosenStrikes {
  const typed = text.trim();
  if (typed === '') return { kind: 'default' };
  if (!/^\d+$/.test(typed)) return { kind: 'refused', problem: 'Enter a whole number of strikes, or leave the box empty.' };
  const digits = typed.replace(/^0+(?=\d)/, '');
  if (most !== null && isMoreThan(digits, most)) return { kind: 'refused', problem: `Strikes each side of the money: the most the server takes is ${most}. Enter ${most} or fewer, or leave the box empty.` };
  return { kind: 'count', digits };
}
