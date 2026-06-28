import { CommonModule } from '@angular/common';
import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import type {
  LifecycleChartAction,
  LifecycleChartActionId,
} from '../../../../api/live-instances.types';

@Component({
  selector: 'app-overview-actions',
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './overview-actions.component.html',
  styleUrl: './overview-actions.component.scss',
})
export class OverviewActionsComponent {
  readonly actions = input.required<LifecycleChartAction[]>();
  readonly busyAction = input<string | null>(null);
  readonly actionInvoked = output<LifecycleChartActionId>();

  invokeAction(action: LifecycleChartAction): void {
    if (!action.enabled || this.busyAction() !== null) return;
    this.actionInvoked.emit(action.id);
  }
}
