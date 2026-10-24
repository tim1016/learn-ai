import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import { BreadcrumbComponent } from '../breadcrumb/breadcrumb.component';
import type { BreadcrumbItem } from '../breadcrumb/breadcrumb-item';
import { WorkspaceActionComponent } from './workspace-action.component';
import type { WorkspaceAction } from './workspace-action';

/** One step of the trail back to the page's list or parent. */
export type WorkspaceBreadcrumbItem = BreadcrumbItem;

/**
 * The header every workspace opens with: breadcrumb, title, purpose line, a
 * facts/chips slot, a help slot and the actions of the active task (PRD #3490
 * R02, R03).
 *
 * The breadcrumb is the page's place in the menu unless the host passes its
 * own: a page inside a group needs no trail written out, and a host with a
 * deeper path (a list, then one record) passes `breadcrumb` to replace it.
 *
 * Slots:
 *   - `[workspaceFacts]` — chips or figures under the purpose line.
 *   - `[workspaceHelp]`  — the "How this works" link, beside the title.
 *
 * Actions are data, not projected markup, so the rule of one prominent action
 * is the shape of the inputs: at most one `primaryAction` for the active task
 * and any number of `secondaryActions`. A read-only view passes neither and
 * the action area is not drawn.
 *
 * Presentation only. It fetches nothing, derives no eligibility, chooses no
 * account, classifies no health and holds no selection: the host passes in
 * every word it shows and handles a routeless action's `actionTriggered`.
 */
@Component({
  selector: 'app-workspace-header',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BreadcrumbComponent, WorkspaceActionComponent],
  templateUrl: './workspace-header.component.html',
  styleUrl: './workspace-header.component.scss',
})
export class WorkspaceHeaderComponent {
  readonly title = input.required<string>();
  /** One line on what this place is for. */
  readonly purpose = input<string | null>(null);
  /** Replaces the menu's trail; `[]` draws none. Omit it to get the menu's. */
  readonly breadcrumb = input<readonly WorkspaceBreadcrumbItem[] | null>(null);
  /** The one prominent action of the active task, or `null` when it has none. */
  readonly primaryAction = input<WorkspaceAction | null>(null);
  readonly secondaryActions = input<readonly WorkspaceAction[]>([]);

  /** The `id` of a routeless action the operator clicked. */
  readonly actionTriggered = output<string>();

  protected readonly hasActions = computed(
    () => this.primaryAction() !== null || this.secondaryActions().length > 0,
  );
}
