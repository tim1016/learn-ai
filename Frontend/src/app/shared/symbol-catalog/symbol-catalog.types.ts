import type { TickerOption } from '../ticker-range-picker/ticker-range-picker.types';

/**
 * One picker row: vendor membership and lake coverage in one value.
 *
 * A `TickerOption` superset, so the picker's existing rows, held-span foot,
 * and window-patching logic keep working unchanged — hosts that hand the
 * card a closed `TickerOption[]` universe never see the difference.
 */
export interface PickerSymbol extends TickerOption {
  /** The vendor lists the symbol but no longer trades it. */
  readonly delisted: boolean;
}

/**
 * Whether a picker row carries a lake-held span. `lastHeld` is absent
 * (undefined) on rows a host universe built without coverage data and
 * explicitly `null` on vendor-only rows — both mean "not held", the gate's
 * subject.
 */
export function isHeldRow(row: TickerOption): boolean {
  return row.lastHeld !== null && row.lastHeld !== undefined;
}

/** A host-supplied `TickerOption[]` universe lifted into picker rows. */
export function toPickerSymbol(option: TickerOption): PickerSymbol {
  return { ...option, delisted: false };
}
