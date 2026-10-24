import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { WorkspaceStateContent } from '../../shared/workspace/workspace-state';
import { WorkspaceStateBlockComponent } from '../../shared/workspace/workspace-state-block.component';
import { operationsTabLabel } from '../operations-tabs';

/** The tabs whose content has not moved into Operations yet. */
type PendingTab = 'math-checks' | 'data-checks';

/** Where each one's content is until it moves, so a visit still has a way forward. */
const WHERE_IT_IS_NOW: Readonly<Record<PendingTab, WorkspaceStateContent['actions']>> = {
  'math-checks': [{ id: 'golden-fixtures', label: 'Open Golden Fixtures', route: ['/golden-fixtures'] }],
  'data-checks': [
    { id: 'validate', label: 'Open CSV compare', route: ['/data-lab/validate'] },
    { id: 'data-divergence', label: 'Open Data divergence', route: ['/research-lab/inspect/data-divergence'] },
    { id: 'pre-flight', label: 'Open Strategy pre-flight', route: ['/research-lab/inspect/pre-flight'] },
  ],
};

/**
 * A tab that arrives in the next step. It says so and points to where the
 * content is today, rather than opening empty.
 */
@Component({
  selector: 'app-operations-pending-tab',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [WorkspaceStateBlockComponent],
  template: '<app-workspace-state-block [state]="state()" />',
})
export class OperationsPendingTabComponent {
  /** From the route's data. */
  readonly tab = input.required<PendingTab>();

  protected readonly state = computed<WorkspaceStateContent>(() => ({
    kind: 'unavailable',
    happened: `${operationsTabLabel(this.tab())} are not in Operations yet.`,
    why: 'They arrive in the next step of this redesign.',
    next: 'Until then, they are where they were.',
    actions: WHERE_IT_IS_NOW[this.tab()],
  }));
}
