/**
 * Recency Chart display-mode window logic (design spec D18-D19).
 *
 * All-symbols mode bounds the visible window to roughly one trading week
 * so many minute-scale lanes stay legible; single-symbol mode (exactly
 * one symbol currently toggled on) unlocks the full accumulated history.
 * The mode is DERIVED from the current symbol-toggle selection, never
 * set independently — narrowing to one symbol is what unlocks the long
 * window.
 */

export type DisplayMode = "all-symbols" | "single-symbol";

/** ~1 trading week: 5 sessions x 6.5h x 60min, in ms. */
export const ALL_SYMBOLS_WINDOW_MS = 5 * 6.5 * 60 * 60_000;

export interface DisplayWindow {
  start: number;
  end: number;
}

export function computeDisplayMode(visibleSymbols: string[]): DisplayMode {
  return visibleSymbols.length === 1 ? "single-symbol" : "all-symbols";
}

export function computeDisplayWindow(
  mode: DisplayMode,
  nowMs: number,
  history: { earliestEntryMs: number | null } = { earliestEntryMs: null },
): DisplayWindow {
  if (mode === "all-symbols") {
    return { start: nowMs - ALL_SYMBOLS_WINDOW_MS, end: nowMs };
  }
  const start = history.earliestEntryMs ?? nowMs - ALL_SYMBOLS_WINDOW_MS;
  return { start: Math.min(start, nowMs - 1), end: nowMs };
}
