import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import type {
  ActionPlan,
  ActionPlanEntryLeg,
  ActionPlanExitEntity,
} from '../../../../api/action-plan.types';
import { isOptionLeg } from '../../../../api/action-plan.types';
import { optionSummary } from '../../../../api/action-plan-format';
import type { OperatorSurfaceActionPlan } from '../../../../api/live-instances.types';

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
  host: {
    '[attr.data-collapsed]': 'collapsedAttr()',
    '[attr.data-verdict]': 'verdictAttr()',
  },
})
export class ActionPlanCardComponent {
  readonly actionPlan = input.required<ActionPlan | null>();
  /** PRD #607 / Slice 5 (#612) — server-authored consumption +
   * anomaly verdict from operator_surface.action_plan.  Drives the
   * one-line summary text and the collapse/verdict-glow attributes. */
  readonly projection = input.required<OperatorSurfaceActionPlan>();
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

  // ─ Slice 5 (#612) — one-line summary + server-driven collapse ───
  readonly consumptionLabel = computed<string>(() => {
    switch (this.projection().consumption) {
      case 'ACTIVE':
        return 'engine-active';
      case 'DECLARATIVE_ONLY':
        return 'declarative only';
      case 'UNKNOWN':
      default:
        return 'activation unknown';
    }
  });

  /** ``N enter · M exit · <consumption label>`` — derived from the
   * declarative leg counts plus the server's consumption enum. */
  readonly oneLineSummary = computed<string>(() => {
    const enters = this.entryLegs().length;
    const exits = this.exitEntities().length;
    return `${enters} enter · ${exits} exit · ${this.consumptionLabel()}`;
  });

  // The override is a single boolean signal (Slice 2 Option A): on
  // attention verdicts the toggle is absent; on READY the operator
  // can manually expand.  The override clears when the server flips
  // the verdict to non-READY.
  private readonly _manuallyExpanded = signal<boolean>(false);
  manualToggle(): void {
    this._manuallyExpanded.update((v) => !v);
  }

  readonly isAttentionCard = computed<boolean>(
    () => this.projection().anomaly_verdict !== 'READY',
  );

  readonly expanded = computed<boolean>(
    () => this.isAttentionCard() || this._manuallyExpanded(),
  );

  readonly collapsedAttr = computed<'true' | 'false'>(() =>
    this.expanded() ? 'false' : 'true',
  );

  readonly verdictAttr = computed<'ready' | 'degraded' | 'unknown'>(() => {
    switch (this.projection().anomaly_verdict) {
      case 'READY':
        return 'ready';
      case 'ATTENTION':
        return 'degraded';
      case 'UNKNOWN':
      default:
        return 'unknown';
    }
  });

  isOption = isOptionLeg;
  optionSummary = optionSummary;
}
