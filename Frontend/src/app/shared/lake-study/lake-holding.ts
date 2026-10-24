/**
 * How much of a lake study's input the lake holds yet, before the study runs (#3521).
 *
 * A lake study fills what the lake lacks, one session at a time, before it computes; a window the lake does not hold
 * yet is a slow first run. This is the one reading every lake study's page shares: the sessions of a span the lake
 * does not hold, from `GET /api/data-lake/instrument-data` (catalog rows and the derived daily file; no bar is read),
 * and the words that warn of them. The counts are Python's: the browser maps them and prints them, and derives no
 * calendar, floor or session from the window.
 */
import type { InstrumentDataReport } from '../data-lake';
import type { WorkspaceStateContent } from '../workspace/workspace-state';

/** The lake trees a capture can write, and so the trees a count can be asked of. */
export type LakeHoldingTree = 'raw' | 'polygon_split_adjusted';

/**
 * What is counted: one instrument's minute trade bars over a span of dates, in one tree. A study with a warmup before
 * its window passes the span it reads, warmup included, so the count is of the sessions the run admits.
 */
export interface LakeHoldingRequest {
  readonly symbol: string;
  /** `YYYY-MM-DD`, both included. */
  readonly from: string;
  readonly to: string;
  readonly tree: LakeHoldingTree;
}

export function sameHoldingRequest(a: LakeHoldingRequest | null, b: LakeHoldingRequest | null): boolean {
  if (a === null || b === null) return a === b;
  return a.symbol === b.symbol && a.from === b.from && a.to === b.to && a.tree === b.tree;
}

/** The span's sessions the lake does not hold, as Python counted them. */
export interface LakeHolding {
  /** Scheduled sessions in the span, finished or not. */
  readonly sessions: number;
  /** Finished sessions a capture can fetch: not in the lake yet. */
  readonly toCapture: number;
  /** Finished sessions not in the lake that a capture cannot fetch now: held by another capture, older than the provider serves, or with no bars. */
  readonly cannotCapture: number;
  /** The span a capture asks for, first through last fetchable session (`int64 ms UTC`, as the backfill takes it); `null` when none. */
  readonly captureSpan: { readonly startMs: number; readonly endMs: number } | null;
}

export function toLakeHolding(report: InstrumentDataReport): LakeHolding {
  const { summary, fill_window: span } = report;
  const notInLake = summary.missing + summary.failed + summary.needs_rebuild + summary.building;
  return {
    sessions: summary.session_count,
    toCapture: summary.fetchable,
    cannotCapture: notInLake - summary.fetchable,
    captureSpan: span ? { startMs: span.start_trading_date_ms, endMs: span.end_trading_date_ms } : null,
  };
}

/** What the count is at: nothing to count yet, being read, read, or not readable. */
export type LakeHoldingReading =
  | { readonly kind: 'idle' }
  | { readonly kind: 'checking' }
  | { readonly kind: 'read'; readonly request: LakeHoldingRequest; readonly holding: LakeHolding }
  | { readonly kind: 'unreadable'; readonly message: string };

/** The one capture that may be running, and how the last one ended when it did not finish. */
export type LakeCapture =
  | { readonly kind: 'idle' }
  | { readonly kind: 'running'; readonly symbol: string; readonly sessions: number }
  | { readonly kind: 'stopped'; readonly cancelled: boolean; readonly message: string };

/** How far a running capture's job has got, in the days of its span that the server counted. */
export interface LakeCaptureProgress {
  readonly done: number;
  readonly total: number;
}

export const CAPTURE_ACTION_ID = 'capture';
export const CANCEL_CAPTURE_ACTION_ID = 'cancel-capture';
export const DISMISS_CAPTURE_ACTION_ID = 'dismiss-capture';

function sessionsText(count: number): string {
  return `${count} ${count === 1 ? 'session' : 'sessions'}`;
}

/**
 * What the page says about the lake's holdings, in the shared state block's words; `null` when it has nothing to say:
 * no count is wanted yet, one is being read, or the lake holds every session the run needs.
 *
 * A capture that is running or has stopped is said first, because it is what the person just did. A count that could
 * not be read says so, with the reason, and never reads as "none missing".
 */
export function describeLakeHolding(
  reading: LakeHoldingReading,
  capture: LakeCapture,
  progress: LakeCaptureProgress | null,
): WorkspaceStateContent | null {
  if (capture.kind === 'running') {
    return {
      kind: 'filling',
      happened: `Capturing ${sessionsText(capture.sessions)} of ${capture.symbol} into the lake.`,
      progress:
        progress === null
          ? { kind: 'phase', label: 'Starting the capture' }
          : { kind: 'counts', done: progress.done, total: progress.total, unit: 'days of the span' },
      next: 'Cancelling stops the capture; what it already wrote stays in the lake. Run study is not held up by it.',
      actions: [{ id: CANCEL_CAPTURE_ACTION_ID, label: 'Cancel the capture' }],
    };
  }
  if (capture.kind === 'stopped') {
    return {
      kind: capture.cancelled ? 'cancelled' : 'failed',
      happened: capture.cancelled ? 'The capture was cancelled.' : 'The capture stopped before it finished.',
      why: capture.cancelled ? null : capture.message,
      next: 'What it wrote stays in the lake. Run study fetches whatever is still missing, one session at a time.',
      actions: [{ id: DISMISS_CAPTURE_ACTION_ID, label: 'Dismiss' }],
    };
  }
  if (reading.kind === 'unreadable') {
    return {
      kind: 'unavailable',
      happened: 'Could not check which of this run’s sessions the lake holds.',
      why: reading.message,
      next: 'Run study still works: it fetches any session the lake lacks, one at a time.',
    };
  }
  if (reading.kind !== 'read') return null;
  const { holding } = reading;
  if (holding.toCapture === 0 && holding.cannotCapture === 0) return null;
  const blocked =
    'Another capture holds them, the provider no longer serves them, or it has no bars for them, so a run that needs them is refused.';
  if (holding.toCapture === 0) {
    return {
      kind: 'partial',
      happened: `${holding.cannotCapture} of ${sessionsText(holding.sessions)} ${holding.cannotCapture === 1 ? 'is' : 'are'} not in the lake and cannot be captured now.`,
      why: blocked,
    };
  }
  const toCapture = holding.toCapture;
  return {
    kind: 'partial',
    happened: `${toCapture} of ${sessionsText(holding.sessions)} ${toCapture === 1 ? 'isn’t' : 'aren’t'} in the lake yet.`,
    why:
      'The first run fetches them one session at a time, so it will be slow.' +
      (holding.cannotCapture > 0 ? ` ${holding.cannotCapture} more cannot be captured now. ${blocked}` : ''),
    next: 'Capture them first.',
    actions: [{ id: CAPTURE_ACTION_ID, label: `Capture ${sessionsText(toCapture)}` }],
  };
}
