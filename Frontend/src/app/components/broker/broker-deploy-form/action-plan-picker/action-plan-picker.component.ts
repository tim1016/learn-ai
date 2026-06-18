import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  effect,
  inject,
  input,
  model,
  signal,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Subject, debounceTime } from 'rxjs';
import type {
  ActionPlan,
  ActionPlanEntryLeg,
  CloseLegExit,
  OptionEntryLeg,
  StockEntryLeg,
} from '../../../../api/action-plan.types';
import { isOptionLeg } from '../../../../api/action-plan.types';
import {
  ActionPlanPreviewService,
  type ParityWarning,
} from '../../../../api/action-plan-preview.service';

/**
 * Deploy-form picker for the operator-declared action plan
 * (PRD #593 Slices 1B + 1C + 1D, issues #595 / #596 / #597).
 *
 * Two sections — "On ENTER" + "On EXIT" — each with ``[+ Add]``. Adding
 * an entry leg auto-fills a mirrored ``close_leg`` reference on the
 * EXIT side; removing the entry cascades. Removing the close_leg only
 * leaves the entry leg untouched.
 *
 * Slice 1D wires parity diagnostics: every plan change debounces a
 * ~150ms call to ``ActionPlanPreviewService.preview`` and renders the
 * returned warning rows inline. **Submit is enabled regardless of
 * warning count** — operator-override is honored. Hard schema errors
 * (Pydantic-rejected, 422) come back through the deploy boundary as
 * before, not through this preview path.
 */
const PREVIEW_DEBOUNCE_MS = 150;

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
  readonly warnings = signal<ParityWarning[]>([]);

  isOption = isOptionLeg;

  private readonly preview = inject(ActionPlanPreviewService);
  private readonly destroyRef = inject(DestroyRef);
  private readonly _changes = new Subject<ActionPlan>();

  constructor() {
    this._changes
      .pipe(debounceTime(PREVIEW_DEBOUNCE_MS), takeUntilDestroyed(this.destroyRef))
      .subscribe((plan) => this._fetchPreview(plan));

    effect(() => {
      this._changes.next(this.actionPlan());
    });
  }

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

  private async _fetchPreview(plan: ActionPlan): Promise<void> {
    try {
      const response = await this.preview.preview(plan);
      this.warnings.set(response.warnings);
    } catch {
      // Preview is best-effort UX sugar; Pydantic is authoritative at
      // submit. A network blip should not block the operator.
      this.warnings.set([]);
    }
  }
}
