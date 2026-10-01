import { ChangeDetectionStrategy, Component, computed, DestroyRef, effect, inject, input, output, signal, untracked } from '@angular/core';
import { ButtonModule } from 'primeng/button';

import { AssetIdentityComponent } from '../../shared/asset-identity/asset-identity.component';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { SymbolPickerComponent } from '../../shared/symbol-picker/symbol-picker.component';
import type { GridSearchRefusal } from '../grid-search/grid-search.types';
import { incumbentLabel } from './golden-search-display';
import { GoldenSearchKnobTableComponent } from './golden-search-knob-table.component';
import { GoldenSearchMethodChoiceComponent } from './golden-search-method-choice.component';
import { applyPlanEdit, DEFAULT_FINAL_MONTHS, draftMonths, MONTH_FIELDS, withServerDates, type PlanDraft, type PlanEdit } from './golden-search-plan-draft';
import { GoldenSearchPreflightPanelComponent } from './golden-search-preflight-panel.component';
import { GoldenSearchProtocolControlsComponent } from './golden-search-protocol-controls.component';
import { GoldenSearchRefinementControlsComponent } from './golden-search-refinement-controls.component';
import { GoldenSearchRefusedError, GoldenSearchService, StageDispatchError, StudyConflictError } from './golden-search.service';
import type { DefaultsMonths, GoldenSearchMethod, GoldenSearchPreflight, StrategyCapability, StudyDetail } from './golden-search.types';
import { IdempotencyKeys } from './idempotency-keys';

/**
 * The Plan step (#2696): choose a strategy with a Golden Search declaration
 * and an instrument, start from the server's defaults, edit the method,
 * knobs and protocol, and lock the plan into a study. Every edit is
 * preflighted (debounced); an answer for an edit that is no longer current is
 * dropped, so Lock always describes what the form shows. Changing the
 * final-test or fold months asks `/defaults` to lay the dates out again (the
 * calendar authority stays in Python) and keeps every other edit; the plan
 * is not checked until the new dates arrive. With `reviseFrom` the form
 * starts from a study's frozen plan and locks a new linked study.
 */
@Component({
  selector: 'app-golden-search-plan-form',
  imports: [
    AssetIdentityComponent,
    ButtonModule,
    GoldenSearchKnobTableComponent,
    GoldenSearchMethodChoiceComponent,
    GoldenSearchPreflightPanelComponent,
    GoldenSearchProtocolControlsComponent,
    GoldenSearchRefinementControlsComponent,
    ReceiptLabelPipe,
    SymbolPickerComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-plan-form.component.html',
  styleUrl: './golden-search-plan-form.component.scss',
})
export class GoldenSearchPlanFormComponent {
  private readonly service = inject(GoldenSearchService);
  private readonly destroyRef = inject(DestroyRef);

  readonly capabilities = input.required<readonly StrategyCapability[]>();
  /** A study whose frozen plan this form revises into a new study. */
  readonly reviseFrom = input<StudyDetail | null>(null);
  /** Debounce between an edit and the server preflight; tests set 0. */
  readonly preflightDebounceMs = input(300);
  /** The id of the study the plan was locked into. */
  readonly locked = output<string>();

  readonly strategyKey = signal<string | null>(null);
  readonly symbol = signal('');
  readonly draft = signal<PlanDraft | null>(null);
  readonly incumbentLabel = signal<string | null>(null);
  readonly loadingDefaults = signal(false);
  readonly defaultsError = signal<string | null>(null);
  readonly preflight = signal<GoldenSearchPreflight | null>(null);
  readonly preflightError = signal<string | null>(null);
  readonly checking = signal(false);
  readonly locking = signal(false);
  readonly lockRefusal = signal<GridSearchRefusal | null>(null);
  readonly lockError = signal<string | null>(null);
  /** The server is laying the dates out for new month counts; the plan waits for them. */
  readonly layingDates = signal(false);
  readonly datesError = signal<string | null>(null);

  protected readonly available = computed(() => this.capabilities().filter((c) => c.available));
  protected readonly unavailable = computed(() => this.capabilities().filter((c) => !c.available));
  protected readonly capability = computed(() => this.capabilities().find((c) => c.strategy_key === this.strategyKey()) ?? null);
  protected readonly revising = computed(() => this.reviseFrom() !== null);
  protected readonly blocked = computed(() => {
    if ((this.draft()?.problems.size ?? 0) > 0) return 'Some values cannot be read yet. Fix them and the plan is checked again.';
    if (this.layingDates()) return 'Laying out the dates for these months…';
    return this.datesError();
  });
  protected readonly canLock = computed(() => {
    const plan = this.preflight();
    return this.draft() !== null && plan !== null && plan.refusals.length === 0 && this.blocked() === null && !this.checking() && !this.locking();
  });

  /** Generation of the latest edit; a preflight that answers an older one is ignored. */
  private preflightGeneration = 0;
  /** Generation of the latest strategy/instrument choice; older defaults are ignored. */
  private defaultsGeneration = 0;
  private debounce: ReturnType<typeof setTimeout> | null = null;
  /** Generation of the latest month counts; dates laid out for older counts are ignored. */
  private datesGeneration = 0;
  private datesDebounce: ReturnType<typeof setTimeout> | null = null;
  private appliedRevision: StudyDetail | null = null;
  private readonly keys = new IdempotencyKeys();

  constructor() {
    effect(() => {
      const revise = this.reviseFrom();
      if (revise !== null) {
        if (revise !== this.appliedRevision) untracked(() => this.startRevision(revise));
        return;
      }
      if (this.appliedRevision !== null) {
        // Out of revise mode: the old study's frozen plan (and incumbent) must not become a new unlinked study.
        untracked(() => {
          this.appliedRevision = null;
          void this.loadDefaults();
        });
        return;
      }
      const available = this.available();
      if (this.strategyKey() === null && available.length > 0) untracked(() => this.selectStrategy(available[0].strategy_key));
    });
    this.destroyRef.onDestroy(() => {
      this.clearDebounce();
      this.clearDatesDebounce();
    });
  }

  selectStrategy(key: string): void {
    this.strategyKey.set(key);
    void this.loadDefaults();
  }

  setSymbol(symbol: string): void {
    this.symbol.set(symbol);
    void this.loadDefaults();
  }

  onStrategyEvent(event: Event): void {
    if (event.target instanceof HTMLSelectElement) this.selectStrategy(event.target.value);
  }

  onMethod(method: GoldenSearchMethod): void {
    this.onEdit({ kind: 'method', method });
  }

  onEdit(edit: PlanEdit): void {
    const draft = this.draft();
    if (draft === null) return;
    // A date typed by hand wins over dates still being laid out for a month count.
    if (edit.kind === 'date') this.cancelDates();
    const next = applyPlanEdit(draft, edit, this.capability());
    this.draft.set(next);
    const months = edit.kind === 'number' && MONTH_FIELDS.has(edit.field) ? draftMonths(next) : null;
    if (months !== null) this.scheduleDates(months);
    else this.scheduleCheck();
  }

  async lock(): Promise<void> {
    const draft = this.draft();
    if (draft === null || !this.canLock()) return;
    const protocol = draft.protocol;
    const revise = this.reviseFrom();
    const key = this.keys.keyFor(JSON.stringify({ revise: revise?.id ?? null, protocol }));
    this.locking.set(true);
    this.lockRefusal.set(null);
    this.lockError.set(null);
    try {
      const outcome =
        revise === null
          ? await this.service.createStudy({ protocol, idempotency_key: key })
          : await this.service.command(revise.id, { command: 'revise', expected_revision: revise.revision, idempotency_key: key, payload: { protocol } });
      this.keys.settle();
      this.locked.emit(outcome.study.id);
    } catch (error) {
      this.onLockFailure(error);
    } finally {
      this.locking.set(false);
    }
  }

  private onLockFailure(error: unknown): void {
    if (error instanceof StageDispatchError) {
      // The study exists; its page explains the stage that did not start.
      this.keys.settle();
      this.locked.emit(error.study.id);
    } else if (error instanceof GoldenSearchRefusedError) {
      this.keys.settle();
      this.lockRefusal.set(error.refusal);
    } else if (error instanceof StudyConflictError) {
      this.keys.settle();
      this.lockError.set(error.message);
    } else {
      // No answer: a retry of the same plan reuses the key, so it cannot lock twice.
      this.lockError.set('The plan was not confirmed as locked. Check the service and press Lock again; the retry cannot create a second study.');
    }
  }

  private startRevision(study: StudyDetail): void {
    this.appliedRevision = study;
    this.defaultsGeneration += 1;
    this.cancelDates();
    this.strategyKey.set(study.strategy_key);
    this.symbol.set(study.symbol);
    this.incumbentLabel.set(incumbentLabel(study.protocol.incumbent));
    this.defaultsError.set(null);
    this.draft.set({ protocol: study.protocol, problems: new Map() });
    this.scheduleCheck();
  }

  private async loadDefaults(): Promise<void> {
    const generation = ++this.defaultsGeneration;
    const strategyKey = this.strategyKey();
    const symbol = this.symbol();
    this.draft.set(null);
    this.cancelDates();
    this.invalidateCheck();
    this.defaultsError.set(null);
    if (strategyKey === null || symbol === '') return;
    this.loadingDefaults.set(true);
    try {
      const { incumbent_label, exposure: _exposure, ...protocol } = await this.service.defaults(strategyKey, symbol);
      if (generation !== this.defaultsGeneration) return;
      this.incumbentLabel.set(incumbent_label);
      this.draft.set({ protocol, problems: new Map(), finalMonths: DEFAULT_FINAL_MONTHS });
      this.scheduleCheck();
    } catch {
      if (generation === this.defaultsGeneration) this.defaultsError.set('The study defaults could not be loaded for this strategy and instrument. Check the service and pick again.');
    } finally {
      if (generation === this.defaultsGeneration) this.loadingDefaults.set(false);
    }
  }

  /** New month counts: after the debounce, the server lays the dates out for them. */
  private scheduleDates(months: DefaultsMonths): void {
    const generation = ++this.datesGeneration;
    this.clearDatesDebounce();
    this.datesError.set(null);
    this.layingDates.set(true);
    this.invalidateCheck();
    this.datesDebounce = setTimeout(() => void this.layOutDates(months, generation), this.preflightDebounceMs());
  }

  private async layOutDates(months: DefaultsMonths, generation: number): Promise<void> {
    const strategyKey = this.strategyKey();
    const symbol = this.symbol();
    if (strategyKey === null || symbol === '') return;
    try {
      const laidOut = await this.service.defaults(strategyKey, symbol, months);
      const draft = this.draft();
      if (generation !== this.datesGeneration || draft === null) return;
      this.layingDates.set(false);
      this.draft.set(withServerDates(draft, laidOut));
      this.scheduleCheck();
    } catch {
      if (generation !== this.datesGeneration) return;
      this.layingDates.set(false);
      this.datesError.set('The dates could not be laid out for these months. Change a month count to try again.');
      this.invalidateCheck();
    }
  }

  /** Forgets any dates still being laid out: the plan they were for is gone. */
  private cancelDates(): void {
    this.datesGeneration += 1;
    this.clearDatesDebounce();
    this.layingDates.set(false);
    this.datesError.set(null);
  }

  private clearDatesDebounce(): void {
    if (this.datesDebounce !== null) clearTimeout(this.datesDebounce);
    this.datesDebounce = null;
  }

  /** Drops whatever was preflighted: it no longer describes the form. */
  private invalidateCheck(): void {
    this.preflightGeneration += 1;
    this.clearDebounce();
    this.preflight.set(null);
    this.preflightError.set(null);
    this.lockRefusal.set(null);
    this.lockError.set(null);
    this.checking.set(false);
  }

  private scheduleCheck(): void {
    this.invalidateCheck();
    if (this.draft() === null || this.blocked() !== null) return;
    this.checking.set(true);
    this.debounce = setTimeout(() => void this.runPreflight(), this.preflightDebounceMs());
  }

  private async runPreflight(): Promise<void> {
    const draft = this.draft();
    if (draft === null) return;
    const generation = this.preflightGeneration;
    try {
      const plan = await this.service.preflight(draft.protocol);
      if (generation !== this.preflightGeneration) return;
      this.preflight.set(plan);
    } catch {
      if (generation === this.preflightGeneration) this.preflightError.set('The plan could not be checked. Check the service and edit again to retry.');
    } finally {
      if (generation === this.preflightGeneration) this.checking.set(false);
    }
  }

  private clearDebounce(): void {
    if (this.debounce !== null) clearTimeout(this.debounce);
    this.debounce = null;
  }
}
