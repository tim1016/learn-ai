import { ChangeDetectionStrategy, Component, computed, DestroyRef, effect, inject, input, linkedSignal, resource, signal, untracked } from '@angular/core';
import { applyEach, disabled, form } from '@angular/forms/signals';
import { ButtonModule } from 'primeng/button';

import { FileDownloadService } from '../../shared/file/file-download.service';
import { PageHeaderComponent } from '../../shared/page-header/page-header.component';
import { extractServerMessage } from '../broker/operation-error';
import { BottomBarComponent, type SaveStatus } from './cards/bottom-bar.component';
import { CostsCardComponent } from './cards/costs-card.component';
import { EntryCardComponent } from './cards/entry-card.component';
import { InstrumentCardComponent } from './cards/instrument-card.component';
import { LegBuilderCardComponent } from './cards/leg-builder-card.component';
import { LegsCardComponent } from './cards/legs-card.component';
import { LegwiseCardComponent } from './cards/legwise-card.component';
import { SideListComponent, type LibraryStatus } from './cards/side-list.component';
import { SummaryComponent } from './cards/summary.component';
import { builderProblems, newLegBuilder, withLegAdded, withLegCopied, withLegRemoved } from './leg-draft';
import { OptionsBacktestService, SetupInvalidError, SetupRefusedError } from './options-backtest.service';
import type { SavedSetup, SessionClock } from './options-backtest.types';
import { buildSetup } from './setup-build';
import { draftFromSetup, newSetupDraft, withDefaultTimes, type SetupDraft } from './setup-draft';
import { acceptedSetup, readSetupFile, setupFileBlob, setupFileName } from './setup-file';
import { isValidSetupName, SETUP_NAME_RULE } from './setup-name';
import { describeDetail, FIELD, NO_PROBLEMS, type FieldProblems } from './setup-problems';
import { SetupSummaryChecker } from './setup-summary-checker';

/** What an import said: it was loaded, or the reasons it was not. Nothing changes when it was not. */
interface ImportNotice {
  readonly kind: 'loaded' | 'refused';
  readonly title: string;
  readonly reasons: readonly string[];
}

/**
 * The Options Backtest page (#3030), part one: build a setup, read it back in
 * plain words, save it, reopen it, delete it, export it to a file and import it
 * from one. The page owns every signal; the cards only show a slice of the
 * form and say what the owner meant.
 */
@Component({
  selector: 'app-options-backtest-page',
  imports: [
    ButtonModule,
    PageHeaderComponent,
    SideListComponent,
    InstrumentCardComponent,
    EntryCardComponent,
    LegwiseCardComponent,
    LegBuilderCardComponent,
    LegsCardComponent,
    CostsCardComponent,
    SummaryComponent,
    BottomBarComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-backtest-page.component.html',
  styleUrl: './options-backtest-page.component.scss',
})
export class OptionsBacktestPageComponent {
  private readonly service = inject(OptionsBacktestService);
  private readonly download = inject(FileDownloadService);

  /** How long the setup rests, in milliseconds, before it is read back. Specs set 0. */
  readonly describeDebounceMs = input(300);

  private readonly clockRead = resource({ loader: () => this.service.sessionClock() });
  /** A full session's open and close. Nothing that holds a time is shown before it is read. */
  protected readonly clock = computed<SessionClock | null>(() => (this.clockRead.hasValue() ? this.clockRead.value() : null));
  protected readonly clockProblem = computed(() => {
    const error: unknown = this.clockRead.error();
    return error === undefined ? null : extractServerMessage(error, 'The session times could not be read.');
  });

  /** The setup being edited. The times fill in from the clock the moment it is read, and only once. */
  protected readonly draft = linkedSignal<SessionClock | null, SetupDraft>({
    source: this.clock,
    computation: (clock, previous) => {
      if (previous === undefined) return newSetupDraft(clock);
      return clock === null ? previous.value : withDefaultTimes(previous.value, clock);
    },
  });
  protected readonly setupForm = form(this.draft, (path) => {
    disabled(path.noReentryClock, { when: ({ valueOf }) => !valueOf(path.noReentryOn) });
    disabled(path.perContractFee, { when: ({ valueOf }) => valueOf(path.feeKind) !== 'per_contract' });
    applyEach(path.legs, (leg) => {
      disabled(leg.stopLossKind, { when: ({ valueOf }) => !valueOf(leg.stopLossOn) });
      disabled(leg.stopLossValue, { when: ({ valueOf }) => !valueOf(leg.stopLossOn) });
      disabled(leg.momentumKind, { when: ({ valueOf }) => !valueOf(leg.momentumOn) });
      disabled(leg.momentumValue, { when: ({ valueOf }) => !valueOf(leg.momentumOn) });
      disabled(leg.reentryKind, { when: ({ valueOf }) => !valueOf(leg.reentryOn) });
      disabled(leg.reentryCount, { when: ({ valueOf }) => !valueOf(leg.reentryOn) });
    });
  });
  protected readonly builder = signal(newLegBuilder());
  protected readonly builderForm = form(this.builder);

  /** The request body for the setup as it stands, or the problems that stop one. */
  private readonly built = computed(() => {
    const clock = this.clock();
    return clock === null ? null : buildSetup(this.draft(), clock);
  });
  private readonly body = computed(() => {
    const built = this.built();
    return built?.ok === true ? built.body : null;
  });

  private readonly checker = new SetupSummaryChecker((body) => this.service.describe(body), () => this.describeDebounceMs());
  protected readonly summary = this.checker.state;
  /** The body the server has read back, once it has read the one the page holds now: what Save and Export send. */
  private readonly readBack = computed(() => {
    const state = this.summary();
    const body = this.body();
    return state.kind === 'ready' && body !== null && JSON.stringify(state.body) === JSON.stringify(body) ? body : null;
  });

  /** Problems at their fields: the page's own first, then the server's. */
  protected readonly fieldProblems = computed<FieldProblems>(() => {
    const built = this.built();
    const state = this.summary();
    const own = built !== null && !built.ok ? built.problems : NO_PROBLEMS;
    const server = state.kind === 'invalid' ? state.problems.fields : NO_PROBLEMS;
    return own.size === 0 ? server : new Map([...server, ...own]);
  });
  protected readonly underlyingProblem = computed(() => this.fieldProblems().get(FIELD.underlying) ?? null);

  // The saved setups.
  private readonly library = resource({ loader: () => this.service.list() });
  protected readonly setups = computed<readonly SavedSetup[]>(() => (this.library.hasValue() ? this.library.value() : []));
  protected readonly libraryStatus = computed<LibraryStatus>(() => {
    if (this.library.error() !== undefined) return 'error';
    return this.library.hasValue() ? 'ready' : 'loading';
  });
  protected readonly libraryProblem = computed(() => {
    const error: unknown = this.library.error();
    return error === undefined ? null : extractServerMessage(error, 'The saved setups could not be read.');
  });
  protected readonly loadedName = signal<string | null>(null);
  protected readonly deleteProblem = signal<string | null>(null);

  // Saving.
  protected readonly saveModel = signal({ name: '' });
  protected readonly saveForm = form(this.saveModel);
  protected readonly saving = signal(false);
  private readonly lastSave = signal<{ readonly stamp: string; readonly status: SaveStatus } | null>(null);
  private readonly stamp = computed(() => `${this.saveModel().name}\n${JSON.stringify(this.body())}`);
  protected readonly nameProblem = computed(() => {
    const name = this.saveModel().name;
    return name === '' || isValidSetupName(name) ? null : SETUP_NAME_RULE;
  });
  protected readonly canSave = computed(() => this.readBack() !== null && this.saveModel().name !== '' && this.nameProblem() === null && !this.saving());
  /** What the last save said, until the name or the setup changes: then it no longer describes what is on screen. */
  protected readonly saveStatus = computed(() => {
    const last = this.lastSave();
    return last !== null && last.stamp === this.stamp() ? last.status : null;
  });
  protected readonly saveHint = computed(() => {
    if (this.saveModel().name === '') return 'Name the setup to save it.';
    return this.readBack() === null ? 'Save is available once the setup reads back cleanly.' : null;
  });

  // Import and export.
  protected readonly importNotice = signal<ImportNotice | null>(null);
  protected readonly canExport = computed(() => this.readBack() !== null);

  constructor() {
    effect(() => {
      const body = this.body();
      untracked(() => this.checker.show(body));
    });
    inject(DestroyRef).onDestroy(() => this.checker.stop());
  }

  protected retryClock(): void {
    this.clockRead.reload();
  }

  protected retryLibrary(): void {
    this.library.reload();
  }

  protected retrySummary(): void {
    this.checker.retry();
  }

  protected open(item: SavedSetup): void {
    const clock = this.clock();
    if (clock === null || item.setup == null) return;
    this.draft.set(draftFromSetup(item.setup, clock));
    this.saveModel.set({ name: item.example ? '' : item.name });
    this.loadedName.set(item.name);
    this.importNotice.set(null);
  }

  protected async remove(name: string): Promise<void> {
    this.deleteProblem.set(null);
    try {
      await this.service.remove(name);
      if (this.loadedName() === name) this.loadedName.set(null);
    } catch (error) {
      this.deleteProblem.set(reasonOf(error, 'The setup could not be deleted. Try again.'));
    }
    this.library.reload();
  }

  protected addLeg(): void {
    if (builderProblems(this.builder()).size === 0) this.draft.update((draft) => withLegAdded(draft, this.builder()));
  }

  protected copyLeg(legId: string): void {
    this.draft.update((draft) => withLegCopied(draft, legId));
  }

  protected removeLeg(legId: string): void {
    this.draft.update((draft) => withLegRemoved(draft, legId));
  }

  protected async save(): Promise<void> {
    const body = this.readBack();
    const name = this.saveModel().name;
    if (body === null || !this.canSave()) return;
    const stamp = this.stamp();
    this.saving.set(true);
    try {
      const saved = await this.service.save(name, body);
      this.lastSave.set({ stamp, status: { kind: 'saved', text: `Saved “${saved.name}”.` } });
      this.loadedName.set(saved.name);
      this.library.reload();
    } catch (error) {
      this.lastSave.set({ stamp, status: { kind: 'refused', text: reasonOf(error, 'The setup could not be saved. Try again.') } });
    } finally {
      this.saving.set(false);
    }
  }

  protected exportSetup(): void {
    const body = this.readBack();
    if (body !== null) this.download.save(setupFileBlob(body), setupFileName(this.saveModel().name, body.underlying));
  }

  protected async importFile(event: Event): Promise<void> {
    const chooser = event.target;
    if (!(chooser instanceof HTMLInputElement)) return;
    const file = chooser.files?.[0] ?? null;
    chooser.value = '';
    const clock = this.clock();
    if (file === null || clock === null) return;
    const read = await readSetupFile(file);
    if (!read.ok) {
      this.importNotice.set({ kind: 'refused', title: `${file.name} was not loaded.`, reasons: [read.message] });
      return;
    }
    try {
      await this.service.describe(read.json);
    } catch (error) {
      this.importNotice.set({ kind: 'refused', title: `${file.name} was not loaded: it is not a setup the page can open.`, reasons: reasonsOf(error) });
      return;
    }
    this.draft.set(draftFromSetup(acceptedSetup(read.json), clock));
    this.saveModel.set({ name: '' });
    this.loadedName.set(null);
    this.importNotice.set({ kind: 'loaded', title: `${file.name} was loaded.`, reasons: [] });
  }
}

function reasonOf(error: unknown, fallback: string): string {
  if (error instanceof SetupRefusedError) return error.message;
  if (error instanceof SetupInvalidError) return `Not saved. ${error.details.map(describeDetail).join(' ')}`;
  return extractServerMessage(error, fallback);
}

function reasonsOf(error: unknown): readonly string[] {
  if (error instanceof SetupInvalidError) return error.details.map(describeDetail);
  return [extractServerMessage(error, 'The server could not read the file. Try again.')];
}
