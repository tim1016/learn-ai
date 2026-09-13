/**
 * The shared Trader/Operator lens vocabulary (task 2026-09-12).
 *
 * One definition serves the Alpaca account desk, the full bot panel, and the
 * triage detail. The lens is a *presentation* dimension only: it never gates
 * authority, and the URL values `trader` | `operator` are user-visible
 * vocabulary — do not rename them.
 */
export type DeskLens = 'trader' | 'operator';

/** The single query-parameter name every routed host reads. */
export const LENS_QUERY_PARAM = 'lens';

/** Display order; also the roving-tabindex order. */
export const DESK_LENSES: readonly DeskLens[] = ['trader', 'operator'];

const LENS_LABELS: Readonly<Record<DeskLens, string>> = {
  trader: 'Trader',
  operator: 'Operator',
};

export function lensLabel(lens: DeskLens): string {
  return LENS_LABELS[lens];
}

/** Strict parser: anything but the two known values recovers to `null`. */
export function parseLens(value: string | null): DeskLens | null {
  return value === 'trader' || value === 'operator' ? value : null;
}

/**
 * Which lens a tablist key press moves to, or `null` when the key is not a
 * lens-transition key. With two lenses, one step right is the whole distance,
 * so ArrowRight/End select Operator and ArrowLeft/Home select Trader — the
 * WAI-ARIA tabs pattern the three hosts already follow. Enter and Space need
 * no helper: native `<button>` activation fires the tab's own click handler.
 */
export function lensFromKey(key: string): DeskLens | null {
  if (key === 'ArrowRight' || key === 'End') return 'operator';
  if (key === 'ArrowLeft' || key === 'Home') return 'trader';
  return null;
}
