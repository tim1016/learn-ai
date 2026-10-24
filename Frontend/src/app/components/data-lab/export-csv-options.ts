/* dataset.csv column and time choices (owner decision 2026-09-19).
 *
 * `unix_ts` is always the first column and is never part of a selection.
 * The Python plan receipt authors the list of selectable data columns
 * (`output_columns`); these pure helpers only track which of them the
 * owner ticked. */

/** `null` — the owner has not edited the checkboxes: every column the plan
 *  lists is exported, including columns from indicators added later.
 *  A list — exactly the ticked columns; columns that appear later start
 *  unticked. */
export type ExportColumnSelection = readonly string[] | null;

/** The selection after ticking or unticking one column. The first edit turns
 *  "everything" into an explicit list of the columns showing now. Pure. */
export function toggleExportColumn(
  selection: ExportColumnSelection,
  available: readonly string[],
  column: string,
  included: boolean,
): readonly string[] {
  const current = selection ?? available;
  if (!included) return current.filter(c => c !== column);
  return current.includes(column) ? current : [...current, column];
}

/** The `columns` field of the generate payload. An explicit selection is
 *  narrowed to the columns the current plan lists (in plan order), so a
 *  column from a since-removed indicator is dropped instead of failing the
 *  run. With no plan loaded the selection passes through and the server
 *  rejects any name it cannot produce. Pure. */
export function columnsForPayload(
  selection: ExportColumnSelection,
  available: readonly string[] | null,
): string[] | null {
  if (selection === null) return null;
  if (available === null) return [...selection];
  const ticked = new Set(selection);
  return available.filter(c => ticked.has(c));
}

/** The browser's IANA zone — the readable time column's default. */
export function browserTimeZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone;
}

/** Zone choices for the readable time column: UTC first, then every IANA
 *  zone the runtime knows. A selected zone missing from that list (a legacy
 *  alias the browser reported) is kept so the dropdown can show it. Pure
 *  over the runtime's zone list. */
export function exportTimeZoneOptions(selected: string | null): readonly string[] {
  const zones = ['UTC', ...Intl.supportedValuesOf('timeZone').filter(z => z !== 'UTC')];
  return selected !== null && !zones.includes(selected) ? [...zones, selected] : zones;
}
