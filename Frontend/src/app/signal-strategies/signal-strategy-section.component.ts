import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type { WorkspaceStateContent } from '../shared/workspace/workspace-state';
import { WorkspaceStateBlockComponent } from '../shared/workspace/workspace-state-block.component';
import type { ReadState } from './read-state';

/**
 * One titled section of a strategy page, with its read's state in front of
 * its content: while the read is on its way it says so, and when it failed it
 * says that the answer is unknown, with a retry. The section's own content is
 * projected and shown by the page only once the read is ready, so a failure is
 * never drawn as an empty list.
 */
@Component({
  selector: 'app-signal-strategy-section',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [WorkspaceStateBlockComponent],
  template: `
    <section class="section">
      <h2 class="section__title">{{ title() }}</h2>
      @if (scope(); as words) {
        <p class="section__scope">{{ words }}</p>
      }
      @if (gate(); as state) {
        <app-workspace-state-block [state]="state" (actionTriggered)="retry.emit()" />
      }
      <ng-content />
    </section>
  `,
  styles: `
    :host { display: block; min-width: 0; }

    .section {
      display: flex;
      flex-direction: column;
      gap: var(--space-2);
      padding: var(--space-3) var(--space-4);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      background: var(--bg-surface);
    }

    .section__title {
      margin: 0;
      color: var(--text-primary);
      font-size: var(--fs-md);
      font-weight: var(--fw-semi);
    }

    .section__scope {
      margin: 0;
      max-width: 60rem;
      color: var(--text-secondary);
      font-size: var(--fs-sm);
    }
  `,
})
export class SignalStrategySectionComponent {
  readonly title = input.required<string>();
  /** What the section's answer covers, and what it does not. */
  readonly scope = input<string | null>(null);
  readonly read = input.required<ReadState<unknown>>();

  readonly retry = output();

  protected readonly gate = computed<WorkspaceStateContent | null>(() => {
    const state = this.read();
    if (state.kind === 'ready') return null;
    if (state.kind === 'loading') {
      return { kind: 'checking', happened: `Reading ${this.title().toLowerCase()}.` };
    }
    return {
      kind: 'failed',
      happened: `${this.title()} could not be read.`,
      why: state.message,
      next: 'Nothing is shown as none or zero because of this: it is unknown until the read succeeds.',
      actions: [{ id: 'retry', label: 'Read it again' }],
    };
  });
}
