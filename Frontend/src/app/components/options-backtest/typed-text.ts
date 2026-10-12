/**
 * Reading what the owner typed. A decimal stays the text typed (the page does
 * no arithmetic on it and never turns it into a number; the server judges its
 * range). A whole number is read to a number only where the schema wants one.
 */

/** The decimal as typed, trimmed, when it is plain digits with an optional fraction; null otherwise. */
export function decimalText(text: string): string | null {
  const trimmed = text.trim();
  return /^\d+(\.\d+)?$/.test(trimmed) ? trimmed : null;
}

/** A whole number written in plain digits (a leading minus allowed), inside the range, or null. */
export function wholeNumber(text: string, min: number, max: number): number | null {
  const trimmed = text.trim();
  if (!/^-?\d+$/.test(trimmed)) return null;
  const value = Number(trimmed);
  return value >= min && value <= max ? value : null;
}
