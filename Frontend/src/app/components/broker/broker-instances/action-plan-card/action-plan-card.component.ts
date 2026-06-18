import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { UpperCasePipe } from '@angular/common';
import type {
  ActionPlan,
  ActionPlanEntryLeg,
  ActionPlanExitEntity,
} from '../../../../api/action-plan.types';

/**
 * Read-only cockpit card that surfaces the bound run's declared action
 * plan (PRD #593 Slice 1A #594, extended in Slice 1B #595). The plan is
 * sourced from ``ledger.live_config.action`` via ``/status``; the engine
 * does NOT consume it in Slices 1–3 (ADR 0012 §"Scope"), so the card
 * carries the explicit literal label to keep the operator honest.
 *
 * Renders nothing when ``actionPlan`` is null — legacy / pre-Slice-1A
 * ledgers must not surface an empty card.
 *
 * Slice 1B adds the stock entry leg + ``close_leg`` row renderings;
 * Slice 1C extends the entry row with right / strike / expiry summaries.
 */
@Component({
  selector: 'app-action-plan-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [UpperCasePipe],
  templateUrl: './action-plan-card.component.html',
  styleUrl: './action-plan-card.component.scss',
})
export class ActionPlanCardComponent {
  readonly actionPlan = input.required<ActionPlan | null>();

  readonly hasPlan = computed<boolean>(() => this.actionPlan() !== null);

  readonly entryLegs = computed<ActionPlanEntryLeg[]>(() => this.actionPlan()?.on_enter ?? []);

  readonly exitEntities = computed<ActionPlanExitEntity[]>(() => this.actionPlan()?.on_exit ?? []);

  readonly entryLegCount = computed<number>(() => this.entryLegs().length);

  readonly exitEntityCount = computed<number>(() => this.exitEntities().length);

  readonly isEmpty = computed<boolean>(
    () => this.hasPlan() && this.entryLegCount() === 0 && this.exitEntityCount() === 0,
  );
}
