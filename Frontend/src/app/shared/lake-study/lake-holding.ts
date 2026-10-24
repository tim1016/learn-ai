/**
 * How much of a lake study's input the lake holds yet, before the study runs (#3521).
 *
 * A lake study fills what the lake lacks, one session at a time, before it computes; a window the lake does not hold
 * yet is a slow first run. This is the one reading every lake study's page shares: the sessions of a window the lake
 * does not hold, from `GET /api/data-lake/instrument-data` (catalog rows and the derived daily file; no bar is read),
 * and the words that warn of them. The counts are Python's: the browser maps them and prints them, and derives no
 * calendar, floor or session from the window.
 */
import type { CaptureProgress, FillWindow } from '../data-lake/capture-fill-window';
import type { InstrumentDataReport, InstrumentDataRequest } from '../data-lake';
import type { WorkspaceStateContent } from '../workspace/workspace-state';

/** The window's sessions the lake does not hold, as Python counted them. */
export interface LakeHolding {
  /** Scheduled sessions in the window, finished or not. */
  readonly sessions: number;
  /** Finished sessions a capture can fetch: not in the lake yet. */
  readonly toCapture: number;
  /** Finished sessions not in the lake that are older than the provider serves: a run that needs one is refused before it fetches anything. */
  readonly beforeProviderHistory: number;
  /** Finished sessions not in the lake that the provider answered with no bars before: a run asks again. */
  readonly providerNoData: number;
  /** Finished sessions another writer holds a claim on now: a run waits for that capture. */
  readonly building: number;
  /** The span a capture asks for, as the server names it; `null` when it names none. */
  readonly captureSpan: FillWindow | null;
}

export function toLakeHolding(report: InstrumentDataReport): LakeHolding {
  const { summary, fill_window: span } = report;
  return {
    sessions: summary.session_count,
    toCapture: summary.fetchable,
    beforeProviderHistory: summary.before_provider_history,
    providerNoData: summary.provider_no_data,
    building: summary.building,
    captureSpan: span ?? null,
  };
}

/** What the count is at: nothing to count yet, being read, read, or not readable. */
export type LakeHoldingReading =
  | { readonly kind: 'idle' }
  | { readonly kind: 'checking' }
  | { readonly kind: 'read'; readonly request: InstrumentDataRequest; readonly holding: LakeHolding }
  | { readonly kind: 'unreadable'; readonly message: string };

/** The one capture that may be running, and how the last one ended when it did not finish. */
export type LakeCapture =
  | { readonly kind: 'idle' }
  | {
      readonly kind: 'running';
      readonly symbol: string;
      readonly sessions: number;
      /** Why the server could not be told to stop the job, when it refused a cancel; the job may still be writing. */
      readonly cancelRefused: string | null;
    }
  | { readonly kind: 'stopped'; readonly cancelled: boolean; readonly message: string };

export const CAPTURE_ACTION_ID = 'capture';
export const CANCEL_CAPTURE_ACTION_ID = 'cancel-capture';
export const DISMISS_CAPTURE_ACTION_ID = 'dismiss-capture';

function sessionsText(count: number): string {
  return `${count} ${count === 1 ? 'session' : 'sessions'}`;
}

/**
 * What a run does with the sessions the lake lacks and a capture cannot fetch, by cause. Each sentence says what the
 * run does (study_inputs admission), not only that the capture leaves them: older than the provider serves is
 * refused before anything is fetched; a day the provider drew no bars for is asked for again; a day another writer
 * holds is waited for.
 */
function cannotCaptureWords(holding: LakeHolding): string[] {
  const { beforeProviderHistory: old, providerNoData: empty, building } = holding;
  const words: string[] = [];
  if (old > 0) {
    words.push(`${sessionsText(old)} ${old === 1 ? 'is' : 'are'} older than the provider serves. A run that needs ${old === 1 ? 'it' : 'them'} is refused, and fetches nothing.`);
  }
  if (empty > 0) {
    words.push(`${sessionsText(empty)} drew no bars from the provider before. A run asks again, and is refused if it still gets none.`);
  }
  if (building > 0) {
    words.push(`${sessionsText(building)} ${building === 1 ? 'is' : 'are'} being captured by another run. A run waits for that capture, and is refused if it does not finish in time.`);
  }
  return words;
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
  progress: CaptureProgress | null,
): WorkspaceStateContent | null {
  if (capture.kind === 'running') {
    return {
      kind: 'filling',
      happened: `Capturing ${sessionsText(capture.sessions)} of ${capture.symbol} into the lake.`,
      progress:
        progress === null
          ? { kind: 'phase', label: 'Starting the capture' }
          : { kind: 'counts', done: progress.done, total: progress.total, unit: 'days of the span' },
      why: capture.cancelRefused,
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
  const blocked = cannotCaptureWords(holding);
  if (holding.toCapture === 0 && blocked.length === 0) return null;
  if (holding.toCapture === 0) {
    const cannot = holding.beforeProviderHistory + holding.providerNoData + holding.building;
    return {
      kind: 'partial',
      happened: `${cannot} of ${sessionsText(holding.sessions)} ${cannot === 1 ? 'is' : 'are'} not in the lake and cannot be captured now.`,
      why: blocked.join(' '),
    };
  }
  const toCapture = holding.toCapture;
  // A run that is refused for a session older than the provider serves fetches nothing, so it is not "slow" either.
  const slow = holding.beforeProviderHistory === 0 ? ['The first run fetches them one session at a time, so it will be slow.'] : [];
  return {
    kind: 'partial',
    happened: `${toCapture} of ${sessionsText(holding.sessions)} ${toCapture === 1 ? 'isn’t' : 'aren’t'} in the lake yet.`,
    why: [...slow, ...(blocked.length > 0 ? ['More cannot be captured now.', ...blocked] : [])].join(' '),
    next: 'Capture them first.',
    actions: [{ id: CAPTURE_ACTION_ID, label: `Capture ${sessionsText(toCapture)}` }],
  };
}
