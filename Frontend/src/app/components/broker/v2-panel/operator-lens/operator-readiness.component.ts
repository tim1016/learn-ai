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
import { ACTION_TONES } from '../bot-detail-banner/lifecycle-action';

type ReadinessCheck = BotPanelView['readiness_checks'][number];

interface ReadinessControl {
  readonly action: PanelAction | null;
  readonly check: ReadinessCheck;
  readonly suppressedBlockerId: string | null;
  readonly suppressedBlockerReasonCode: string | null;
  readonly tone: PanelActionTone;
}

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
  /**
   * The Operator lens's backend-selected banner action
   * (`primary_action_by_lens.operator`, issue #1665). Its gate remains
   * visible below; the accordion row for this exact operation is suppressed
   * here to avoid duplicating the banner's control.
   */
  readonly bannerActionId = input<ActionId | null>(null);
  readonly actionPending = input(false);
  readonly actionRequested = output<PanelActionTrigger>();

  protected readonly readinessControls = computed<readonly ReadinessControl[]>(
    () => {
      const panel = this.panel();
      const actions = new Map(
        panel.actions.map((action) => [action.action_id, action] as const),
      );

      return panel.readiness_checks.map((check) => {
        const tone = ACTION_TONES[check.operation];
        const action = tone && check.operation !== this.bannerActionId()
          ? actions.get(check.operation) ?? null
          : null;

        const firstBlockerId = action?.blockers[0]?.condition.id ?? null;
        return {
          action,
          check,
          // The gate owns the operator prose. A disabled action still exposes
          // its stable reason code beside the compact action control.
          suppressedBlockerId: firstBlockerId,
          suppressedBlockerReasonCode: action?.enabled ? null : firstBlockerId,
          tone: tone ?? 'neutral',
        };
      });
    },
  );
}
