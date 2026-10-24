import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { WorkspaceAction, WorkspaceActionVariant } from './workspace-action';

/**
 * One action, drawn as a link when it has a route and as a button when the
 * host handles it. Used by the workspace header and the state block, so an
 * action looks the same wherever it is offered.
 */
@Component({
  selector: 'app-workspace-action',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  templateUrl: './workspace-action.component.html',
  styleUrl: './workspace-action.component.scss',
})
export class WorkspaceActionComponent {
  readonly action = input.required<WorkspaceAction>();
  readonly variant = input<WorkspaceActionVariant>('secondary');

  /** The routeless action's `id`, for the host to handle. */
  readonly triggered = output<string>();

  /** The route to navigate to, or `null` for a button. A disabled action is
   * a disabled button even when it has a route, so it cannot navigate. */
  protected readonly linkRoute = computed(() => {
    const action = this.action();
    return action.disabled === true ? null : (action.route ?? null);
  });
}
