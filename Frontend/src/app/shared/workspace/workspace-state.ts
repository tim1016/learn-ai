import type { WorkspaceAction } from './workspace-action';

/** Every state a workspace view can be in (PRD #3490 R04), where it applies. */
export type WorkspaceStateKind =
  | 'idle'
  | 'invalid'
  | 'checking'
  | 'filling'
  | 'queued'
  | 'running'
  | 'complete'
  | 'partial'
  | 'cancelled'
  | 'failed'
  | 'unavailable';

/** The state's name, drawn as the block's heading. It names the state and
 * nothing about why the view is in it: the reason is the domain's to give. */
export const WORKSPACE_STATE_LABELS: Readonly<Record<WorkspaceStateKind, string>> = {
  idle: 'Idle',
  invalid: 'Invalid input',
  checking: 'Checking data',
  filling: 'Filling data',
  queued: 'Queued',
  running: 'Running',
  complete: 'Complete',
  partial: 'Partial',
  cancelled: 'Cancelled',
  failed: 'Failed',
  unavailable: 'Unavailable',
};

/**
 * How far along work is, in what the server counted or named.
 *
 * There is no percent field on purpose. A count is "12 of 30 days" and a phase
 * is the step the work is in; a total the server does not know is `null`, and
 * the block then says how many are done so far instead of guessing a share.
 */
export type WorkspaceStateProgress =
  | {
      readonly kind: 'counts';
      readonly done: number;
      /** `null` when the total is not known yet. */
      readonly total: number | null;
      /** What is being counted: "days", "sessions", "contracts". */
      readonly unit: string;
    }
  | { readonly kind: 'phase'; readonly label: string };

/**
 * What the domain says about the state it is in.
 *
 * The block renders these words and adds none: `happened` is what happened,
 * `why` is the domain's reason, `next` is what the operator can do. A reason
 * the domain did not give is left out, not replaced by a guess.
 */
export interface WorkspaceStateContent {
  readonly kind: WorkspaceStateKind;
  readonly happened: string;
  readonly why?: string | null;
  readonly next?: string | null;
  readonly progress?: WorkspaceStateProgress | null;
  /** The correct retry or follow-up, e.g. "Retry fill" separately from "Retry computation". */
  readonly actions?: readonly WorkspaceAction[];
}
