/** Shared display-only duration formatting for run and preservation receipts. */
export function formatDurationMs(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  const seconds = Math.floor(Math.max(0, value) / 1000);
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, '0')}s`;
}
