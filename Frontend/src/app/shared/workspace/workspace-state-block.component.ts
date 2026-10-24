import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import { WorkspaceActionComponent } from './workspace-action.component';
import {
  WORKSPACE_STATE_LABELS,
  type WorkspaceStateContent,
  type WorkspaceStateKind,
  type WorkspaceStateProgress,
} from './workspace-state';

/** The states that interrupt the reader: work that did not produce an answer. */
const ALERT_STATES: ReadonlySet<WorkspaceStateKind> = new Set<WorkspaceStateKind>(['failed', 'unavailable']);

/** How strongly a state is drawn; the words, not the colour, carry the state. */
type StateTone = 'neutral' | 'good' | 'warn' | 'bad';

const STATE_TONES: Readonly<Record<WorkspaceStateKind, StateTone>> = {
  idle: 'neutral',
  invalid: 'warn',
  checking: 'neutral',
  filling: 'neutral',
  queued: 'neutral',
  running: 'neutral',
  complete: 'good',
  partial: 'warn',
  cancelled: 'neutral',
  failed: 'bad',
  unavailable: 'bad',
};

function progressText(progress: WorkspaceStateProgress): string {
  if (progress.kind === 'phase') return progress.label;
  return progress.total === null
    ? `${progress.done} ${progress.unit} so far`
    : `${progress.done} of ${progress.total} ${progress.unit}`;
}

/**
 * One view's state, in the domain's own words (PRD #3490 R04): what happened,
 * why, and what the operator can do — for idle, invalid input, checking data,
 * filling data, queued, running, complete, partial, cancelled, failed and
 * unavailable.
 *
 * The block never invents a reason: it prints what `state` carries and leaves
 * out what is absent. Progress is the server's counts or phase, never a
 * percentage. Its actions are the host's and are all drawn secondary, so the
 * header keeps the one prominent action of the task (R03).
 */
@Component({
  selector: 'app-workspace-state-block',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [WorkspaceActionComponent],
  templateUrl: './workspace-state-block.component.html',
  styleUrl: './workspace-state-block.component.scss',
})
export class WorkspaceStateBlockComponent {
  readonly state = input.required<WorkspaceStateContent>();

  /** The `id` of a routeless action the operator clicked. */
  readonly actionTriggered = output<string>();

  protected readonly kindLabel = computed(() => WORKSPACE_STATE_LABELS[this.state().kind]);
  protected readonly tone = computed(() => STATE_TONES[this.state().kind]);
  protected readonly role = computed(() => (ALERT_STATES.has(this.state().kind) ? 'alert' : 'status'));

  protected readonly actions = computed(() => this.state().actions ?? []);

  /** The server's progress as text, plus a bar only where a bar is true: a
   * known, non-empty total. Nothing here turns counts into a percentage. */
  protected readonly progress = computed(() => {
    const progress = this.state().progress ?? null;
    if (progress === null) return null;
    const bar =
      progress.kind === 'counts' && progress.total !== null && progress.total > 0
        ? { done: progress.done, total: progress.total }
        : null;
    return { text: progressText(progress), bar };
  });
}
