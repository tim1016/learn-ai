import type { DataSourceProvenance, DataSourceWindow } from '../../shared/workspace/data-source-provenance';
import type { StoredStudyRecord } from './stored-study';

function text(value: unknown): string | null {
  return typeof value === 'string' && value !== '' ? value : null;
}

function windowOf(start: unknown, end: unknown): DataSourceWindow | null {
  return typeof start === 'number' && typeof end === 'number' ? { startMs: start, endMs: end } : null;
}

/**
 * The data-source line of a saved run, from what the record carries and nothing else.
 *
 * `null` when the record carries no provenance, and the line then reads "not recorded": a run saved before its study
 * returned a `data_source` block is not given today's provider, because the data it read may not be what the study
 * reads now. When the result carried a block, each field is read if it is there and left unrecorded if it is not; no
 * field is inferred from the instrument or the window. A run whose request carried its data (Realized vs IV) has the
 * digest of that data as its identity.
 */
export function storedDataSource(record: StoredStudyRecord): DataSourceProvenance | null {
  const block: Record<string, unknown> | null = record.data_source;
  if (block === null && record.data_identity === null) return null;
  const window = windowOf(block?.['window_start_ms'], block?.['window_end_ms']);
  const effective = block?.['effective_window'] as Record<string, unknown> | null | undefined;
  const warmup = block?.['warmup_sessions'];
  const minutes = block?.['bar_minutes'];
  const provider = text(block?.['provider']) ?? text(block?.['kind']);
  return {
    sources: provider === null ? [] : [provider],
    subject: text(block?.['symbol']),
    requestedWindow: window,
    effectiveWindow: windowOf(effective?.['start_ms'], effective?.['end_ms']),
    warmup: typeof warmup === 'number' ? `${warmup} sessions` : null,
    resolution: typeof minutes === 'number' ? `${minutes} minute${minutes === 1 ? '' : 's'}` : null,
    adjustment: text(block?.['price_adjustment_mode']),
    sessionPolicy: text(block?.['session_policy']),
    dataIdentity: text(block?.['snapshot_digest']) ?? text(block?.['identity_digest']) ?? record.data_identity,
    observedAtMs: null,
    counts: null,
    limitations: [],
  };
}
