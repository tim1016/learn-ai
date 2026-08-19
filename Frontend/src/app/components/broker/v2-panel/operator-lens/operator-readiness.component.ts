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
  readonly suppressedBlockerReasonCode: string | null;
  readonly tone: PanelActionTone;
}

interface PrimaryReadinessControl extends ReadinessControl {
  readonly action: PanelAction;
}

const OPERATOR_ACTION_TONES: Partial<Record<ActionId, PanelActionTone>> = {
  resume: 'primary',
  pause: 'warning',
  continue: 'primary',
  stop: 'danger',
  stop_bot_decisions: 'danger',
  flatten_stop: 'danger',
  reconcile_now: 'neutral',
  recover_exact_execution_evidence: 'warning',
  resolve_execution_coverage: 'warning',
  cancel_verified_working_orders: 'danger',
  prepare_safe_flatten: 'neutral',
  open_custody_timeline: 'neutral',
  rebuild_from_mirror: 'warning',
  reset_authority: 'danger',
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
  /** The one lifecycle control promoted to the banner; its gate remains visible below. */
  readonly bannerActionId = input<ActionId | null>(null);
  readonly actionPending = input(false);
  readonly actionRequested = output<PanelActionTrigger>();

  /** A policy-primary recovery must never be hidden behind an accordion row. */
  protected readonly primaryControl = computed<PrimaryReadinessControl | null>(() => {
    const panel = this.panel();
    const check = panel.readiness_checks.find(
      (candidate) => candidate.evidence['primary'] === true,
    );
    if (check === undefined || check.operation === this.bannerActionId()) return null;
    const tone = OPERATOR_ACTION_TONES[check.operation];
    if (tone === undefined) return null;
    const action = panel.actions.find((candidate) => candidate.action_id === check.operation);
    if (action === undefined) return null;
    return {
      action,
      check,
      suppressedBlockerId: null,
      suppressedBlockerReasonCode: null,
      tone,
    };
  });

  protected readonly readinessControls = computed<readonly ReadinessControl[]>(
    () => {
      const panel = this.panel();
      const actions = new Map(
        panel.actions.map((action) => [action.action_id, action] as const),
      );

      return panel.readiness_checks.map((check) => {
        const tone = OPERATOR_ACTION_TONES[check.operation];
        const isPrimary = check.evidence['primary'] === true;
        const action = tone && check.operation !== this.bannerActionId()
          && !isPrimary
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
