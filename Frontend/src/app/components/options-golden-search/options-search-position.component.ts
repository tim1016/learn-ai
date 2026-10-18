import { ChangeDetectionStrategy, Component, computed, effect, inject, input, output, resource, signal } from '@angular/core';
import { form, FormField } from '@angular/forms/signals';

import { LegsCardComponent } from '../options-backtest/cards/legs-card.component';
import { builderProblems, newLegBuilder, withLegAdded, withLegCopied, withLegRemoved } from '../options-backtest/leg-draft';
import { OptionsBacktestService } from '../options-backtest/options-backtest.service';
import { PositionPreviewComponent } from '../options-backtest/position/position-preview.component';
import { buildSetup } from '../options-backtest/setup-build';
import { draftFromSetup, newSetupDraft } from '../options-backtest/setup-draft';
import { optionsSearchErrorMessage, OptionsGoldenSearchService } from './options-golden-search.service';
import type { SearchDeclarations } from './options-golden-search.types';

/** Edits the prospective seed. Search still pins an explicitly saved, server-authored setup hash. */
@Component({
  selector: 'app-options-search-position',
  imports: [FormField, LegsCardComponent, PositionPreviewComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-search-position.component.html',
  styleUrl: './options-search-position.component.scss',
})
export class OptionsSearchPositionComponent {
  readonly declarations = input.required<SearchDeclarations>();
  readonly startingDate = input('');
  readonly dirty = output<boolean>();
  readonly saved = output<SearchDeclarations>();
  private readonly setups = inject(OptionsBacktestService);
  private readonly searches = inject(OptionsGoldenSearchService);
  private readonly clock = resource({ loader: () => this.setups.sessionClock() });
  private readonly limits = resource({ loader: () => this.setups.limits() });
  protected readonly draft = signal(newSetupDraft(null));
  protected readonly fields = form(this.draft);
  protected readonly builder = signal(newLegBuilder());
  protected readonly builderFields = form(this.builder);
  protected readonly identity = signal({ name: '' });
  protected readonly identityFields = form(this.identity);
  protected readonly busy = signal(false);
  protected readonly problem = signal<string | null>(null);
  protected readonly servedLimits = computed(() => this.limits.hasValue() ? this.limits.value() : null);
  protected readonly clockProblem = computed(() => this.clock.error() === undefined ? null : 'The session clock could not be read. Retry the position editor.');
  protected retryClock(): void { this.clock.reload(); }
  protected readonly ready = computed(() => this.clock.hasValue());
  private original = '';
  protected readonly changed = computed(() => this.ready() && this.original !== JSON.stringify(this.draft()));
  protected readonly built = computed(() => this.clock.hasValue() ? buildSetup(this.draft(), this.clock.value(), this.servedLimits()) : null);
  protected readonly setup = computed(() => { const built = this.built(); return built?.ok ? built.body : null; });

  constructor() {
    effect(() => {
      const declarations = this.declarations();
      if (!this.clock.hasValue()) return;
      const draft = draftFromSetup(declarations.setup, this.clock.value());
      this.original = JSON.stringify(draft);
      this.draft.set(draft);
      this.identity.set({ name: declarations.setup_name + ' edited' });
    });
    effect(() => { this.dirty.emit(this.changed()); });
  }
  protected add(): void {
    if (builderProblems(this.builder(), this.servedLimits()).size === 0) this.draft.update(value => withLegAdded(value, this.builder(), this.servedLimits()));
  }
  protected copy(id: string): void { this.draft.update(value => withLegCopied(value, id, this.servedLimits())); }
  protected remove(id: string): void { this.draft.update(value => withLegRemoved(value, id)); }
  protected async save(): Promise<void> {
    const setup = this.setup();
    const name = this.identity().name.trim();
    if (setup === null || name === '' || this.busy()) return;
    const stamp = JSON.stringify(this.draft());
    const originalHash = this.declarations().setup_hash;
    this.busy.set(true); this.problem.set(null);
    try {
      await this.setups.save(name, setup);
      const declarations = await this.searches.declarations(name);
      if (originalHash === this.declarations().setup_hash && stamp === JSON.stringify(this.draft())) this.saved.emit(declarations);
      else this.problem.set('The earlier position was saved, but newer edits remain. Save them before searching.');
    } catch (error) { this.problem.set(optionsSearchErrorMessage(error, 'The edited position could not be saved.')); }
    finally { this.busy.set(false); }
  }
}
