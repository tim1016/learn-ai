import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import type { ActionPlan } from '../../../../api/action-plan.types';

/**
 * Read-only cockpit card that surfaces the bound run's declared action
 * plan (PRD #593 Slice 1A, issue #594). The plan is sourced from
 * ``ledger.live_config.action`` via ``/status``; the engine does NOT
 * consume it in Slices 1–3 (ADR 0012 §"Scope"), so the card carries the
 * explicit literal label below to keep the operator honest about state.
 *
 * Renders nothing when ``actionPlan`` is null — legacy / pre-Slice-1A
 * ledgers must not surface an empty card that suggests a plan was
 * declared when in fact the field pre-dates the ledger.
 *
 * Leg shapes (stock, option) land in #595 / #596; Slice 1A only ships
 * the empty-state.
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

  readonly entryLegCount = computed<number>(() => this.actionPlan()?.on_enter.length ?? 0);

  readonly exitEntityCount = computed<number>(() => this.actionPlan()?.on_exit.length ?? 0);

  readonly isEmpty = computed<boolean>(
    () => this.hasPlan() && this.entryLegCount() === 0 && this.exitEntityCount() === 0,
  );
}
