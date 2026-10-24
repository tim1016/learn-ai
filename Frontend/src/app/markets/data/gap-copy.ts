import { formatReceiptLabel } from '../../shared/pipes/receipt-label.pipe';
import type { GapCause, SessionState } from './instrument-data';

/** What each state a session can be in is called on this tab. */
export const SESSION_STATE_LABELS: Readonly<Record<SessionState, string>> = {
  ready: 'Ready',
  building: 'Being written',
  needs_rebuild: 'Needs rebuild',
  missing: 'Missing',
  failed: 'Fetch failed',
  forming: 'Still forming',
  upcoming: 'Not started',
};

/** What a run refuses about a session the catalog calls complete, in the words of the evidence it lacks. */
const REBUILD_REASONS: Readonly<Record<string, string>> = {
  price_file_missing: 'its price file is not on disk',
  adjustment_record_missing: 'its adjustment record is missing',
  adjustment_record_rejected: 'its adjustment record does not match its price file',
  catalog_marked_stale: 'the catalog marked it stale',
};

/**
 * Why a gap is one, and what a Fill does about it, in one sentence.
 *
 * The causes are Python's (`state`, `reason`, `blocked_by`); the words are
 * these. A reason this build does not know goes through the shared receipt
 * label, so it still reaches the reader.
 */
export function gapCauseText(cause: GapCause): string {
  const older = cause.blockedBy === 'before_provider_history';
  switch (cause.state) {
    case 'missing':
      return older
        ? 'Not in the lake, and older than the provider serves, so it can no longer be fetched.'
        : 'Not in the lake. A Fill fetches it.';
    case 'needs_rebuild': {
      const lacks = cause.reason === null ? 'a file it needs' : (REBUILD_REASONS[cause.reason] ?? formatReceiptLabel(cause.reason));
      return older
        ? `The catalog calls it complete, but a run refuses it: ${lacks}. It is older than the provider serves, so it cannot be rebuilt.`
        : `The catalog calls it complete, but a run refuses it: ${lacks}. A Fill fetches it again.`;
    }
    case 'failed':
      if (cause.blockedBy === 'provider_no_data') {
        return 'The provider returned no bars for it, as when a symbol had not listed yet. A Fill does not count it, but asks the provider again if it lies inside the span the Fill covers.';
      }
      if (older) return 'The last fetch failed, and it is older than the provider serves, so it can no longer be fetched.';
      return `The last fetch failed${cause.reason === null ? '' : ` (${formatReceiptLabel(cause.reason)})`}. A Fill tries again.`;
    case 'building':
      return 'Another run is writing it now.';
    case 'ready':
    case 'forming':
    case 'upcoming':
      return '';
  }
}
