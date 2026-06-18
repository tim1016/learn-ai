import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { RouterLink } from '@angular/router';
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
  imports: [RouterLink],
  templateUrl: './action-plan-card.component.html',
  styleUrl: './action-plan-card.component.scss',
})
export class ActionPlanCardComponent {
  readonly actionPlan = input.required<ActionPlan | null>();
  /** Slice 1E (#598) — when the bound run carries an identity to deep-
   * link from, the card renders a "Redeploy with changes" CTA that
   * navigates to the deploy form with ``parent_run_id`` pre-set in the
   * query params. ``null`` (default) hides the CTA — pre-Slice-1E
   * ledgers don't carry the parent. */
  readonly parentRunId = input<string | null>(null);

  readonly redeployQueryParams = computed<Record<string, string> | null>(() => {
    const id = this.parentRunId();
    return id ? { parent_run_id: id } : null;
  });

  readonly hasPlan = computed<boolean>(() => this.actionPlan() !== null);

  readonly entryLegs = computed<ActionPlanEntryLeg[]>(() => this.actionPlan()?.on_enter ?? []);

  readonly exitEntities = computed<ActionPlanExitEntity[]>(() => this.actionPlan()?.on_exit ?? []);

  readonly isEmpty = computed<boolean>(
    () => this.hasPlan() && this.entryLegs().length === 0 && this.exitEntities().length === 0,
  );

  isOption = isOptionLeg;
  optionSummary = optionSummary;
}
