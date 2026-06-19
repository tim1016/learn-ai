import { ChangeDetectionStrategy, Component, computed, input, model } from '@angular/core';
import type {
  ActionPlan,
  ActionPlanEntryLeg,
  CloseLegExit,
  OptionEntryLeg,
  StockEntryLeg,
} from '../../../../api/action-plan.types';
import { isOptionLeg } from '../../../../api/action-plan.types';

/**
 * Deploy-form picker for the operator-declared action plan
 * (PRD #593 Slices 1B + 1C, issues #595 / #596).
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
 * Slice 1C: distinct ``[+ Add stock]`` / ``[+ Add option]`` buttons;
 * option legs spawn with sensible defaults (long call, ATM strike,
 * min_dte 14d). The picker exposes the option-specific row so the
 * cockpit can show it conditionally — actual selector-editing controls
 * are kept minimal in 1B/1C (operator can edit in JSON-ish form via the
 * preview endpoint once Slice 1D lands).
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

  readonly entryLegs = computed<ActionPlanEntryLeg[]>(() => this.actionPlan().on_enter);
  readonly exitEntities = computed<CloseLegExit[]>(() => this.actionPlan().on_exit);

  /** Type guard exposed to the template so ``@if (isOption(leg))`` works
   * as a discriminating predicate inside the ``@for`` loop. */
  isOption = isOptionLeg;

  addStockEntry(): void {
    const newLeg: StockEntryLeg = {
      leg_id: this._nextLegId(),
      instrument: { kind: 'stock', underlying: this.prefillUnderlying() ?? '' },
      position: 'long',
      qty_ratio: 1,
    };
    this._appendEntry(newLeg);
  }

  addOptionEntry(): void {
    const newLeg: OptionEntryLeg = {
      leg_id: this._nextLegId(),
      instrument: { kind: 'option', underlying: this.prefillUnderlying() ?? '' },
      position: 'long',
      qty_ratio: 1,
      right: 'call',
      strike: { selector: 'atm' },
      expiry: { selector: 'min_dte', days: 14 },
    };
    this._appendEntry(newLeg);
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

  private _appendEntry(leg: ActionPlanEntryLeg): void {
    const mirroredExit: CloseLegExit = { kind: 'close_leg', entry_leg_id: leg.leg_id };
    const current = this.actionPlan();
    this.actionPlan.set({
      on_enter: [...current.on_enter, leg],
      on_exit: [...current.on_exit, mirroredExit],
    });
  }
}
