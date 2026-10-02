/**
 * The gate one viewer last chose for a strategy, kept in this browser only
 * (#2639). A per-viewer convenience: when storage is blocked (a private
 * window, denied site data) the view simply opens on the strategy's default
 * gate, and a choice still applies for the rest of the visit.
 */
const STORAGE_PREFIX = 'broker-v2.strategy-view.gate.v1:';

/** The stored gate id, or `null` when none is stored or storage cannot be read. */
export function readGatePreference(strategyKey: string): string | null {
  try {
    return localStorage.getItem(`${STORAGE_PREFIX}${strategyKey}`);
  } catch {
    // Storage unreadable: the caller falls back to the declaration's default gate.
    return null;
  }
}

/** Remember a choice, when this browser will store it. */
export function writeGatePreference(strategyKey: string, gateId: string): void {
  try {
    localStorage.setItem(`${STORAGE_PREFIX}${strategyKey}`, gateId);
  } catch {
    // Storage full or blocked: the choice applies for this visit only.
  }
}
