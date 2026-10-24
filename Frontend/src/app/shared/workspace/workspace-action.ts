/**
 * One action a workspace piece can offer, as the host describes it.
 *
 * The pieces render actions; they never decide what an action does. An action
 * with a `route` navigates. An action without one is a button, and the piece
 * that rendered it reports the click as the action's `id` for the host to
 * handle: submitting a job, cancelling it and retrying a fill stay with the
 * page that owns them.
 */
export interface WorkspaceAction {
  /** Stable identity, reported when a routeless action is clicked. */
  readonly id: string;
  /** Names what the action does: "Run backtest", not "Run". */
  readonly label: string;
  /** Router commands. Present: the action is a link. Absent: the host handles it. */
  readonly route?: readonly string[];
  readonly queryParams?: Readonly<Record<string, string>>;
  /** Shown but not usable. The host says why elsewhere (the state block). */
  readonly disabled?: boolean;
}

/** `primary` is the one prominent action of the active task (PRD #3490 R03). */
export type WorkspaceActionVariant = 'primary' | 'secondary';
