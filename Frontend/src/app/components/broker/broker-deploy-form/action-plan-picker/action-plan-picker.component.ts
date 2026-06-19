import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  input,
  model,
  signal,
} from '@angular/core';
import { takeUntilDestroyed, toObservable } from '@angular/core/rxjs-interop';
import { debounceTime } from 'rxjs';
import type {
  ActionPlan,
  ActionPlanEntryLeg,
  CloseLegExit,
  ExpirySelector,
  OptionEntryLeg,
  StockEntryLeg,
  StrikeSelector,
} from '../../../../api/action-plan.types';
import { isOptionLeg } from '../../../../api/action-plan.types';
import {
  ActionPlanPreviewService,
  type ParityWarning,
} from '../../../../api/action-plan-preview.service';
import type {
  OptionContractMatch,
  SymbolMatch,
} from '../../../../api/broker-models';
import { BrokerInstrumentCardComponent } from '../../../../shared/broker-instrument-card/broker-instrument-card.component';
import { OptionLegPickerComponent } from './option-leg-picker/option-leg-picker.component';

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
  imports: [BrokerInstrumentCardComponent, OptionLegPickerComponent],
  templateUrl: './action-plan-picker.component.html',
  styleUrl: './action-plan-picker.component.scss',
})
export class ActionPlanPickerComponent {
  readonly actionPlan = model.required<ActionPlan>();
  readonly prefillUnderlying = input<string | null>(null);

  readonly entryLegs = computed<ActionPlanEntryLeg[]>(() => this.actionPlan().on_enter);
  readonly exitEntities = computed<CloseLegExit[]>(() => this.actionPlan().on_exit);
  readonly warnings = signal<ParityWarning[]>([]);

  /** Broker-coupled picker workflow as a tagged union so the symbol
   * captured during the option-add flow can never drift apart from the
   * picker stage. ``intent`` discriminates the "Add stock" vs "Add
   * option" entry path; the drill state carries the already-picked
   * underlying so the template never needs a null narrowing dance. */
  readonly pickerState = signal<
    | { mode: 'idle' }
    | { mode: 'symbol'; intent: 'stock' | 'option' }
    | { mode: 'drill'; symbol: SymbolMatch }
  >({ mode: 'idle' });

  isOption = isOptionLeg;

  formatStrike(strike: StrikeSelector): string {
    switch (strike.selector) {
      case 'atm':
        return 'ATM';
      case 'atm_offset':
        return `ATM${strike.offset >= 0 ? '+' : ''}${strike.offset}`;
      case 'absolute':
        return `$${strike.strike}`;
    }
  }

  formatExpiry(expiry: ExpirySelector): string {
    switch (expiry.selector) {
      case 'min_dte':
        return `${expiry.days}d+`;
      case 'nearest_weekly':
        return 'weekly';
      case 'absolute':
        return new Date(expiry.expiration_ms).toISOString().slice(0, 10);
    }
  }

  private readonly preview = inject(ActionPlanPreviewService);

  constructor() {
    // One transformation: the actionPlan signal IS the source. No
    // Subject + effect() bridge. ``toObservable`` runs the
    // signal-to-Rx conversion in an injection context (the constructor)
    // and ``takeUntilDestroyed`` ties the subscription lifetime to the
    // component without an explicit DestroyRef plumb-through.
    toObservable(this.actionPlan)
      .pipe(debounceTime(PREVIEW_DEBOUNCE_MS), takeUntilDestroyed(inject(DestroyRef)))
      .subscribe((plan) => this._fetchPreview(plan));
  }

  beginAddStock(): void {
    this.pickerState.set({ mode: 'symbol', intent: 'stock' });
  }

  beginAddOption(): void {
    this.pickerState.set({ mode: 'symbol', intent: 'option' });
  }

  cancelPicker(): void {
    this.pickerState.set({ mode: 'idle' });
  }

  onSymbolPicked(match: SymbolMatch): void {
    const state = this.pickerState();
    if (state.mode !== 'symbol') return;
    if (state.intent === 'stock') {
      const newLeg: StockEntryLeg = {
        leg_id: this._nextLegId(),
        instrument: { kind: 'stock', underlying: match.symbol },
        position: 'long',
        qty_ratio: 1,
      };
      this._appendEntry(newLeg);
      this.pickerState.set({ mode: 'idle' });
      return;
    }
    this.pickerState.set({ mode: 'drill', symbol: match });
  }

  onOptionLegQualified(match: OptionContractMatch): void {
    // Schema accepts ``absolute`` selectors so the broker-qualified
    // strike + expiry round-trips into ``run_id`` exactly as the
    // operator picked them. Slice 4's resolver will re-qualify against
    // the live broker; the persisted ``con_id`` lives unhashed in the
    // ledger alongside the leg.
    const newLeg: OptionEntryLeg = {
      leg_id: this._nextLegId(),
      instrument: { kind: 'option', underlying: match.symbol },
      position: 'long',
      qty_ratio: 1,
      right: match.right === 'C' ? 'call' : 'put',
      strike: { selector: 'absolute', strike: match.strike },
      expiry: { selector: 'absolute', expiration_ms: match.expiry_ms },
    };
    this._appendEntry(newLeg);
    this.pickerState.set({ mode: 'idle' });
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
