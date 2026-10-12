/** What the owner has put into the four controls of the Past chain tab, as typed. */
export interface ChainControls {
  /** The underlying's ticker; empty while none is picked. */
  underlying: string;
  /** The trading date as `YYYY-MM-DD`; empty while the field holds no usable date. */
  date: string;
  /** A New York clock time as `HH:MM`; empty means the session's close. */
  time: string;
  /** Strikes each side of the money as digits; empty means the server's own number. */
  strikes: string;
}

export const NO_CONTROLS: ChainControls = { underlying: '', date: '', time: '', strikes: '' };

/** What the strikes box says: nothing (the server's own number), a count as the digits typed, or a refusal. */
export type ChosenStrikes =
  | { readonly kind: 'default' }
  | { readonly kind: 'count'; readonly digits: string }
  | { readonly kind: 'refused'; readonly problem: string };

/**
 * The strikes are sent as the digits typed (no leading zeros), never as a number the page made. How many the server
 * allows is the server's to say: a count it refuses comes back as a refusal, shown at this box.
 */
export function chosenStrikes(text: string): ChosenStrikes {
  const typed = text.trim();
  if (typed === '') return { kind: 'default' };
  if (!/^\d+$/.test(typed)) return { kind: 'refused', problem: 'Enter a whole number of strikes, or leave the box empty.' };
  return { kind: 'count', digits: typed.replace(/^0+(?=\d)/, '') };
}
