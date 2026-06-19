import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import type {
  ActionPlan,
  ActionPlanEntryLeg,
  ActionPlanExitEntity,
} from '../../../../api/action-plan.types';
import { isOptionLeg } from '../../../../api/action-plan.types';
import { optionSummary } from '../../../../api/action-plan-format';

/**
 * Read-only cockpit card that surfaces the bound run's declared action
 * plan (PRD #593, ADR 0012). The plan is sourced from
 * ``ledger.live_config.action`` via ``/status``; the engine does NOT
 * consume it in Slices 1–3, so the card carries the explicit literal
 * label to keep the operator honest.
 *
 * Renders nothing when ``actionPlan`` is null — legacy / pre-Slice-1A
 * ledgers must not surface an empty card.
 *
 * Slice 1A — empty-state label.
 * Slice 1B — stock entry leg + close_leg row rendering.
 * Slice 1C — option entry leg with human-readable selector summaries
 *            (shared formatters in ``api/action-plan-format.ts``).
 */
@Component({
  selector: 'app-action-plan-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './action-plan-card.component.html',
  styleUrl: './action-plan-card.component.scss',
})
export class ActionPlanCardComponent {
  readonly actionPlan = input.required<ActionPlan | null>();

  readonly hasPlan = computed<boolean>(() => this.actionPlan() !== null);

  readonly entryLegs = computed<ActionPlanEntryLeg[]>(() => this.actionPlan()?.on_enter ?? []);

  readonly exitEntities = computed<ActionPlanExitEntity[]>(() => this.actionPlan()?.on_exit ?? []);

  readonly isEmpty = computed<boolean>(
    () => this.hasPlan() && this.entryLegs().length === 0 && this.exitEntities().length === 0,
  );

  isOption = isOptionLeg;
  optionSummary = optionSummary;
}
