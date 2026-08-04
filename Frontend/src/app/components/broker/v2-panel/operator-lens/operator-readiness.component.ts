import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
} from '@angular/core';
import { Tooltip } from 'primeng/tooltip';
import {
  Accordion,
  AccordionContent,
  AccordionHeader,
  AccordionPanel,
} from 'primeng/accordion';
import type {
  ActionId,
  BotPanelView,
  PanelAction,
  PanelActionTrigger,
} from '../lib/broker-v2-panel.types';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import {
  PanelActionButtonComponent,
  type PanelActionTone,
} from '../panel-action-button/panel-action-button.component';

type ReadinessCheck = BotPanelView['readiness_checks'][number];

interface ReadinessControl {
  readonly action: PanelAction | null;
  readonly check: ReadinessCheck;
  readonly suppressedBlockerId: string | null;
  readonly tone: PanelActionTone;
}

const OPERATOR_ACTION_TONES: Partial<Record<ActionId, PanelActionTone>> = {
  resume: 'primary',
  pause: 'warning',
  continue: 'primary',
  stop: 'danger',
  flatten_stop: 'danger',
  reconcile_now: 'neutral',
  clear_hold: 'warning',
  record_inventory_baseline: 'warning',
};

@Component({
  selector: 'app-operator-readiness',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    Accordion,
    AccordionContent,
    AccordionHeader,
    AccordionPanel,
    PanelActionButtonComponent,
    ReceiptLabelPipe,
    Tooltip,
  ],
  templateUrl: './operator-readiness.component.html',
  styleUrl: './operator-readiness.component.scss',
})
export class OperatorReadinessComponent {
  readonly panel = input.required<BotPanelView>();
  readonly actionPending = input(false);
  readonly actionRequested = output<PanelActionTrigger>();

  protected readonly readinessControls = computed<readonly ReadinessControl[]>(
    () => {
      const panel = this.panel();
      const actions = new Map(
        panel.actions.map((action) => [action.action_id, action] as const),
      );

      return panel.readiness_checks.map((check) => {
        const tone = OPERATOR_ACTION_TONES[check.operation];
        const action = tone
          ? actions.get(check.operation) ?? null
          : null;

        return {
          action,
          check,
          suppressedBlockerId: action?.blockers[0]?.condition.id ?? null,
          tone: tone ?? 'neutral',
        };
      });
    },
  );
}
