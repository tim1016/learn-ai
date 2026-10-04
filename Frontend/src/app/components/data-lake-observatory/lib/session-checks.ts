import { formatReceiptLabel } from '../../../shared/pipes/receipt-label.pipe';
import type { PriceAdjustmentMode } from '../../../shared/data-lake';
import type { CoverageCell } from './coverage-board';
import { needsAdjustmentRecord } from './lake-readiness';

/** `ok` holds, `bad` is what stops a run, `muted` does not apply or cannot be told yet. */
export type CheckTone = 'ok' | 'bad' | 'muted';

export interface SessionCheck {
  readonly label: string;
  readonly tone: CheckTone;
  readonly detail: string;
}

export interface SessionVerdict {
  readonly readable: boolean;
  readonly title: string;
  /** What fixes it; empty when a run can read the session. */
  readonly remedy: string;
}

function priceFile(cell: CoverageCell): SessionCheck {
  const label = 'Price file';
  if (cell.status === 'missing' || cell.rebuildReason === 'price_file_missing') {
    return { label, tone: 'bad', detail: 'Not on disk' };
  }
  if (cell.status === 'complete' || cell.status === 'needs_rebuild') {
    return { label, tone: 'ok', detail: 'On disk' };
  }
  return { label, tone: 'muted', detail: 'Not committed' };
}

function adjustmentRecord(cell: CoverageCell, mode: PriceAdjustmentMode): SessionCheck {
  const label = 'Adjustment record';
  if (!needsAdjustmentRecord(mode)) {
    return { label, tone: 'muted', detail: 'Not needed in this view' };
  }
  if (cell.status === 'complete') return { label, tone: 'ok', detail: 'Current' };
  switch (cell.rebuildReason) {
    case 'adjustment_record_missing':
      return { label, tone: 'bad', detail: 'Missing' };
    case 'adjustment_record_rejected':
      return { label, tone: 'bad', detail: 'Out of date, or does not match the price file' };
    default:
      return { label, tone: 'muted', detail: 'Written with the price file' };
  }
}

function catalogEntry(cell: CoverageCell): SessionCheck {
  const label = 'Catalog entry';
  if (cell.status === 'missing') return { label, tone: 'bad', detail: 'None' };
  if (cell.status === 'complete' || cell.status === 'needs_rebuild') {
    return { label, tone: 'ok', detail: 'Complete' };
  }
  return { label, tone: 'bad', detail: formatReceiptLabel(cell.status) };
}

/** The three things a run needs for one session, in the order a backfill writes them. */
export function sessionChecks(cell: CoverageCell, mode: PriceAdjustmentMode): readonly SessionCheck[] {
  return [priceFile(cell), adjustmentRecord(cell, mode), catalogEntry(cell)];
}

export function sessionVerdict(cell: CoverageCell): SessionVerdict {
  if (cell.status === 'complete') {
    return { readable: true, title: 'A run can read this session', remedy: '' };
  }
  const title = "A run can't read this session";
  if (cell.status === 'fetching') return { readable: false, title, remedy: 'A backfill is writing it now.' };
  if (cell.status === 'needs_rebuild' && cell.rebuildReason !== 'price_file_missing') {
    return {
      readable: false,
      title,
      remedy: 'A backfill fetches the session again and writes its adjustment record.',
    };
  }
  return { readable: false, title, remedy: 'A backfill fetches it.' };
}
