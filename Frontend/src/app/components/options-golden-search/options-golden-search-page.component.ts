import { ChangeDetectionStrategy, Component, computed, DestroyRef, effect, inject, input, resource, signal, untracked } from '@angular/core';
import { form, FormField } from '@angular/forms/signals';
import { Router, RouterLink } from '@angular/router';

import { AssetIdentityComponent } from '../../shared/asset-identity';
import type { DateRange } from '../../shared/date-picker';
import { PageHeaderComponent } from '../../shared/page-header/page-header.component';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { rangeInstants } from '../options-backtest/duration';
import { OptionsBacktestService } from '../options-backtest/options-backtest.service';
import { OptionsSearchKnobsComponent, type KnobSelection } from './options-search-knobs.component';
import { OptionsSearchRulesComponent, type RuleSelection } from './options-search-rules.component';
import { OptionsSearchStudyComponent, type StudyAction } from './options-search-study.component';
import { OptionsGoldenSearchService, OptionsSearchDispatchError, optionsSearchErrorMessage } from './options-golden-search.service';
import type { OptionsStudy, PlanRequest, Preflight, SearchCommand, SearchDeclarations, SessionLoader } from './options-golden-search.types';

const STEPS = ['Setup', 'Search settings', 'Rules and history', 'Research', 'Compare and final test', 'Approval'];

@Component({
  selector: 'app-options-golden-search-page',
  imports: [AssetIdentityComponent, FormField, PageHeaderComponent, ReceiptLabelPipe, RouterLink, OptionsSearchStudyComponent, OptionsSearchKnobsComponent, OptionsSearchRulesComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-golden-search-page.component.html',
  styleUrl: './options-golden-search.component.scss',
})
export class OptionsGoldenSearchPageComponent {
  private readonly service = inject(OptionsGoldenSearchService);
  private readonly setupsService = inject(OptionsBacktestService);
  private readonly router = inject(Router);
  readonly studyId = input<string | undefined>(undefined);
  readonly setup_name = input<string | undefined>(undefined);
  readonly parent_study_id = input<string | undefined>(undefined);
  protected readonly steps = STEPS;
  protected readonly sessionLoader: SessionLoader = (studyId, candidateHash, window, sessionOpenMs) => this.service.session(studyId, candidateHash, window, sessionOpenMs);
  protected readonly step = signal(0);
  protected readonly selection = signal({ name: '' });
  protected readonly fields = form(this.selection);
  protected readonly declarations = signal<SearchDeclarations | null>(null);
  protected readonly study = signal<OptionsStudy | null>(null);
  protected readonly error = signal<string | null>(null);
  protected readonly busy = signal(false);
  protected readonly loading = signal(false);
  protected readonly range = signal<DateRange>({ from: '', to: '' });
  protected readonly knobs = signal<readonly KnobSelection[]>([]);
  protected readonly rules = signal<RuleSelection | null>(null);
  protected readonly checked = signal<{ readonly stamp: string; readonly value: Preflight } | null>(null);
  protected readonly library = resource({ loader: () => this.setupsService.list() });
  protected readonly history = resource({ loader: () => this.service.list() });
  protected readonly savedSetups = computed(() => this.library.hasValue() ? this.library.value() : []);
  protected readonly studies = computed(() => this.history.hasValue() ? this.history.value() : []);
  protected readonly libraryError = computed(() => this.library.error() === undefined ? null : optionsSearchErrorMessage(this.library.error(), 'Saved setups could not be loaded.'));
  protected readonly historyError = computed(() => this.history.error() === undefined ? null : optionsSearchErrorMessage(this.history.error(), 'Study history could not be loaded.'));
  protected readonly request = computed<PlanRequest | null>(() => {
    const declarations = this.declarations();
    const rules = this.rules();
    const range = rangeInstants(this.range());
    if (declarations === null || rules === null || range === null || this.knobs().length === 0) return null;
    return { setup_name: declarations.setup_name, expected_setup_hash: declarations.setup_hash, ...(this.parent_study_id() ? { parent_study_id: this.parent_study_id() } : {}), knobs: this.knobs(), method: rules.method, policy: rules.policy, history: { ...range, train_months: rules.train_months, test_months: rules.test_months, final_months: rules.final_months }, budget: rules.budget, finalist_count: rules.finalist_count, stress: rules.stress, zoom: rules.zoom, pair_audits: rules.pair_audits };
  });
  protected readonly preflight = computed(() => this.checked()?.stamp === JSON.stringify(this.request()) ? this.checked()?.value ?? null : null);
  protected readonly canLock = computed(() => this.request() !== null && this.preflight()?.can_lock === true && !this.busy());
  private generation = 0;
  private readonly keys = new Map<string, string>();
  private readonly destroyRef = inject(DestroyRef);

  constructor() {
    effect(() => {
      const id = this.studyId();
      const name = this.setup_name();
      untracked(() => {
        if (id) void this.loadStudy(id);
        else if (name) {
          this.selection.update((value) => ({ ...value, name }));
          void this.loadSetup(name);
        }
      });
    });
    effect((onCleanup) => {
      const value = this.study();
      if (value === null || !value.allowed_commands.includes('cancel')) return;
      const timer = setInterval(() => { if (!this.busy()) void this.refresh(); }, 2500);
      onCleanup(() => clearInterval(timer));
    });
    this.destroyRef.onDestroy(() => { this.generation += 1; });
  }

  protected chooseStep(step: number): void {
    if (this.study() === null && step > 2) return;
    this.step.set(step);
  }

  protected async chooseSetup(): Promise<void> {
    const name = this.selection().name;
    if (name !== '') await this.loadSetup(name);
  }

  private async loadSetup(name: string): Promise<void> {
    const generation = ++this.generation;
    this.loading.set(true);
    this.error.set(null);
    this.declarations.set(null);
    this.checked.set(null);
    this.knobs.set([]);
    this.rules.set(null);
    try {
      const declarations = await this.service.declarations(name);
      if (generation === this.generation) this.declarations.set(declarations);
    } catch (error) {
      if (generation === this.generation) this.error.set(optionsSearchErrorMessage(error, 'This saved setup could not be prepared for search.'));
    } finally {
      if (generation === this.generation) this.loading.set(false);
    }
  }

  protected async checkPlan(): Promise<void> {
    const request = this.request();
    if (request === null || this.busy()) return;
    const stamp = JSON.stringify(request);
    this.busy.set(true);
    this.error.set(null);
    try {
      const value = await this.service.preflight(request);
      if (JSON.stringify(this.request()) === stamp) this.checked.set({ stamp, value });
    } catch (error) {
      this.error.set(optionsSearchErrorMessage(error, 'Preflight could not verify the plan.'));
    } finally { this.busy.set(false); }
  }

  protected async lock(): Promise<void> {
    const request = this.request();
    const checked = this.preflight();
    if (!this.canLock() || request === null || checked === null) return;
    this.busy.set(true);
    this.error.set(null);
    try {
      const study = await this.service.create(request, checked.preflight_hash, this.key(`lock:${checked.preflight_hash}`));
      this.acceptStudy(study);
      this.step.set(3);
      await this.router.navigate(['/options-golden-search', study.id]);
    } catch (error) {
      this.error.set(optionsSearchErrorMessage(error, 'The plan could not be locked.'));
    } finally { this.busy.set(false); }
  }

  private async loadStudy(id: string): Promise<void> {
    const generation = ++this.generation;
    this.loading.set(true);
    this.error.set(null);
    try {
      const study = await this.service.get(id);
      if (generation !== this.generation) return;
      this.acceptStudy(study);
      this.step.set(study.final_results.length > 0 ? 5 : study.candidates.length > 0 ? 4 : 3);
    } catch (error) {
      if (generation === this.generation) this.error.set(optionsSearchErrorMessage(error, 'The study could not be loaded.'));
    } finally {
      if (generation === this.generation) this.loading.set(false);
    }
  }

  protected async refresh(): Promise<void> {
    const current = this.study();
    if (current === null) return;
    try {
      const value = await this.service.get(current.id);
      if (this.study()?.id === current.id && value.revision >= (this.study()?.revision ?? 0)) this.acceptStudy(value);
    } catch (error) { this.error.set(optionsSearchErrorMessage(error, 'The latest study state could not be read.')); }
  }

  protected allowed(command: SearchCommand): boolean {
    return !this.busy() && this.study()?.allowed_commands.includes(command) === true;
  }

  protected async command(action: StudyAction): Promise<void> {
    const study = this.study();
    if (study === null || !this.allowed(action.command)) return;
    const { command, ...details } = action;
    this.busy.set(true);
    this.error.set(null);
    try {
      const value = await this.service.command(study.id, { command, expected_revision: study.revision, idempotency_key: this.key(`${study.id}:${study.revision}:${command}:${JSON.stringify(details)}`), ...details });
      this.acceptStudy(value);
      if (command === 'open_final') this.step.set(5);
    } catch (error) {
      if (error instanceof OptionsSearchDispatchError) this.acceptStudy(error.study);
      this.error.set(error instanceof OptionsSearchDispatchError ? error.message : optionsSearchErrorMessage(error, 'The command did not complete. Refresh to read the latest durable state.'));
    } finally { this.busy.set(false); }
  }

  private acceptStudy(study: OptionsStudy): void {
    this.study.set(study);
  }

  private key(intent: string): string {
    const existing = this.keys.get(intent);
    if (existing !== undefined) return existing;
    const key = crypto.randomUUID();
    this.keys.set(intent, key);
    return key;
  }
}
