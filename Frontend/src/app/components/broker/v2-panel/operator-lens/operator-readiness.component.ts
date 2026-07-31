import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
} from '@angular/core';
import type { BotPanelView, PanelAction } from '../lib/broker-v2-panel.types';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { PanelActionButtonComponent } from '../panel-action-button/panel-action-button.component';

@Component({
  selector: 'app-operator-readiness',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PanelActionButtonComponent, ReceiptLabelPipe],
  templateUrl: './operator-readiness.component.html',
  styleUrl: './operator-readiness.component.scss',
})
export class OperatorReadinessComponent {
  readonly panel = input.required<BotPanelView>();
  readonly actionPending = input(false);
  readonly actionRequested = output<PanelAction>();

  protected readonly flattenStopAction = computed(() =>
    this.findAction('flatten_stop'),
  );
  protected readonly reconcileAction = computed(() =>
    this.findAction('reconcile_now'),
  );
  protected readonly clearHoldAction = computed(() =>
    this.findAction('clear_hold'),
  );
  protected readonly inventoryBaselineAction = computed(() =>
    this.findAction('record_inventory_baseline'),
  );

  private findAction(actionId: PanelAction['action_id']): PanelAction | null {
    return this.panel().actions.find((action) => action.action_id === actionId) ?? null;
  }
}
