import { ChangeDetectionStrategy, Component, computed, input, model } from '@angular/core';
import type {
  ActionPlan,
  CloseLegExit,
  StockEntryLeg,
} from '../../../../api/action-plan.types';

/**
 * Deploy-form picker for the operator-declared action plan
 * (PRD #593 Slice 1B, issue #595).
 *
 * Two sections — "On ENTER" + "On EXIT" — each with ``[+ Add]``. Adding
 * a stock entry leg auto-fills a mirrored ``close_leg`` reference on the
 * EXIT side; removing the entry cascades. Removing the close_leg only
 * leaves the entry leg untouched. Operator can edit either side
 * independently.
 *
 * ``prefillUnderlying`` is UX sugar (e.g. ``live_config.symbol``) — it
 * fills the new leg's ``instrument.underlying`` at add time, but the
 * stored leg always carries the literal value (no implicit
 * context-dependence at the wire format; ADR 0012 §5).
 *
 * Slice 1C (#596) will extend the entry row with right / strike / expiry
 * fields when ``instrument.kind`` is ``option``.
 */
@Component({
  selector: 'app-action-plan-picker',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './action-plan-picker.component.html',
  styleUrl: './action-plan-picker.component.scss',
})
export class ActionPlanPickerComponent {
  readonly actionPlan = model.required<ActionPlan>();
  readonly prefillUnderlying = input<string | null>(null);

  readonly entryLegs = computed<StockEntryLeg[]>(() => this.actionPlan().on_enter);
  readonly exitEntities = computed<CloseLegExit[]>(() => this.actionPlan().on_exit);

  addStockEntry(): void {
    const legId = this._nextLegId();
    const newLeg: StockEntryLeg = {
      leg_id: legId,
      instrument: { kind: 'stock', underlying: this.prefillUnderlying() ?? '' },
      position: 'long',
      qty_ratio: 1,
    };
    const mirroredExit: CloseLegExit = { kind: 'close_leg', entry_leg_id: legId };
    const current = this.actionPlan();
    this.actionPlan.set({
      on_enter: [...current.on_enter, newLeg],
      on_exit: [...current.on_exit, mirroredExit],
    });
  }

  removeEntry(legId: string): void {
    const current = this.actionPlan();
    this.actionPlan.set({
      on_enter: current.on_enter.filter((leg) => leg.leg_id !== legId),
      on_exit: current.on_exit.filter((exit) => exit.entry_leg_id !== legId),
    });
  }

  removeExit(entryLegId: string): void {
    const current = this.actionPlan();
    this.actionPlan.set({
      on_enter: current.on_enter,
      on_exit: current.on_exit.filter((exit) => exit.entry_leg_id !== entryLegId),
    });
  }

  /** Auto-assigned ids count from ``leg_1``; the operator may rename to
   * anything matching ``^[a-z0-9_]{1,32}$`` later. Avoids collisions with
   * already-declared ids. */
  private _nextLegId(): string {
    const taken = new Set(this.entryLegs().map((l) => l.leg_id));
    let i = 1;
    while (taken.has(`leg_${i}`)) i += 1;
    return `leg_${i}`;
  }
}
