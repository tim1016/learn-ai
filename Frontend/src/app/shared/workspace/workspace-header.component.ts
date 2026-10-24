import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { RouterLink } from '@angular/router';

import { WorkspaceActionComponent } from './workspace-action.component';
import type { WorkspaceAction } from './workspace-action';

/** One step of the trail back to the page's list or parent. */
export interface WorkspaceBreadcrumbItem {
  readonly label: string;
  /** Router commands. Absent: the item is plain text, as the current level is. */
  readonly route?: readonly string[];
  /** Carries the list's filters and paging back with the link. */
  readonly queryParams?: Readonly<Record<string, string>>;
}

/**
 * The header every workspace opens with: breadcrumb, title, purpose line, a
 * facts/chips slot, a help slot and the actions of the active task (PRD #3490
 * R02, R03).
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
  imports: [RouterLink, WorkspaceActionComponent],
  templateUrl: './workspace-header.component.html',
  styleUrl: './workspace-header.component.scss',
})
export class WorkspaceHeaderComponent {
  readonly title = input.required<string>();
  /** One line on what this place is for. */
  readonly purpose = input<string | null>(null);
  readonly breadcrumb = input<readonly WorkspaceBreadcrumbItem[]>([]);
  /** The one prominent action of the active task, or `null` when it has none. */
  readonly primaryAction = input<WorkspaceAction | null>(null);
  readonly secondaryActions = input<readonly WorkspaceAction[]>([]);

  /** The `id` of a routeless action the operator clicked. */
  readonly actionTriggered = output<string>();

  protected readonly hasActions = computed(
    () => this.primaryAction() !== null || this.secondaryActions().length > 0,
  );
}
