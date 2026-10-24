import { computed, effect, inject, Injectable, InjectionToken, linkedSignal, resource, signal, untracked } from '@angular/core';
import { applyEach, disabled, form, max, min } from '@angular/forms/signals';

import { extractServerMessage } from '../../components/broker/operation-error';
import { builderProblems, newLegBuilder, withLegAdded, withLegCopied, withLegRemoved } from '../../components/options-backtest/leg-draft';
import { OptionsBacktestService, SetupInvalidError, SetupRefusedError } from '../../components/options-backtest/options-backtest.service';
import type { SavedSetup, ServedSetup, SessionClock, SetupSummary } from '../../components/options-backtest/options-backtest.types';
import { RunNameTracker } from '../../components/options-backtest/run-name';
import { buildSetup } from '../../components/options-backtest/setup-build';
import { draftFromSetup, newSetupDraft, withDefaultTimes, type SetupDraft } from '../../components/options-backtest/setup-draft';
import { readSetupFile, setupFileBlob, setupFileName } from '../../components/options-backtest/setup-file';
import { nameProblem } from '../../components/options-backtest/setup-limits';
import { generatedSetupName } from '../../components/options-backtest/setup-name';
import { describeDetail, FIELD, NO_PROBLEMS, type FieldProblems } from '../../components/options-backtest/setup-problems';
import { SetupSummaryChecker } from '../../components/options-backtest/setup-summary-checker';
import type { SaveStatus } from '../../components/options-backtest/cards/bottom-bar.component';
import { FileDownloadService } from '../../shared/file/file-download.service';

/** How long the setup rests, in milliseconds, before the server reads it back. A spec that is not about the wait sets 0. */
export const SETUP_DESCRIBE_DEBOUNCE_MS = new InjectionToken<number>('SETUP_DESCRIBE_DEBOUNCE_MS', {
  providedIn: 'root',
  factory: () => 300,
});

export type LibraryStatus = 'loading' | 'ready' | 'error';

/** What an import said: it was loaded, or the reasons it was not. Nothing changes when it was not. */
export interface ImportNotice {
  readonly kind: 'loaded' | 'refused';
  readonly title: string;
  readonly reasons: readonly string[];
}

/**
 * What the address names, as far as the library says.
 *
 * `draft` is the address of a setup with no saved name. For a name, the
 * library either has it (`open`), is still being read or could not be read,
 * has no setup by that name (`unknown`), or has an entry that can no longer be
 * opened and says why (`cannot-open`). Nothing opens another setup instead.
 */
export type SetupEntry =
  | { readonly kind: 'draft' }
  | { readonly kind: 'loading' }
  | { readonly kind: 'failed'; readonly reason: string }
  | { readonly kind: 'unknown' }
  | { readonly kind: 'cannot-open'; readonly reason: string }
  | { readonly kind: 'open'; readonly item: SavedSetup | null };

/**
 * Whether the draft still matches the content last saved or opened under its
 * name. A draft with no name has nothing to match.
 */
export type DraftStanding =
  | { readonly kind: 'unsaved' }
  | { readonly kind: 'matches'; readonly name: string; readonly example: boolean }
  | { readonly kind: 'changed'; readonly name: string; readonly example: boolean };

/**
 * The working copy of one options setup, kept alive across the workspace's tabs
 * (Overview, Define and the tabs that follow). The workspace provides one, and
 * its address (a saved name, or none) says which setup it works on.
 *
 * It holds what the Options Backtest page held for the setup itself: the form,
 * its read-back by the server, the saved-name library and the name a run is
 * recorded under, and none of what a run needs. Opening a setup, saving it,
 * importing a file and exporting one live here, so that a tab switch loses
 * nothing and a later tab can ask for the setup exactly as the server read it.
 */
@Injectable()
export class OptionsSetupWorkspaceStore {
  private readonly service = inject(OptionsBacktestService);
  private readonly download = inject(FileDownloadService);
  private readonly describeDebounceMs = inject(SETUP_DESCRIBE_DEBOUNCE_MS);

  /** The saved name the address carries, or `null` for the unsaved draft. The workspace sets it from the URL. */
  readonly address = signal<string | null>(null);
  /**
   * Where the draft says it lives now, after a save under a name or a replacement
   * that has none. The workspace follows it with the URL; the draft never waits on it.
   */
  readonly home = signal<{ readonly name: string | null } | null>(null);
  /**
   * The address the draft has been made to match: opened from the library, or already the draft itself. An address
   * is acted on once, when it changes, so no later read of the library, and no save or import that leaves the
   * address behind for a moment, can open the setup again over an edit.
   */
  private acknowledged: string | null | undefined;

  /** What the server holds a setup to. Until it is read, or when it cannot be, nothing is checked here and the server judges. */
  private readonly limitsRead = resource({ loader: () => this.service.limits() });
  readonly limits = computed(() => (this.limitsRead.hasValue() ? this.limitsRead.value() : null));
  readonly limitsProblem = computed(() => {
    const error: unknown = this.limitsRead.error();
    return error === undefined ? null : extractServerMessage(error, 'The server did not answer.');
  });

  private readonly clockRead = resource({ loader: () => this.service.sessionClock() });
  /** A full session's open and close. Nothing that holds a time is shown before it is read. */
  readonly clock = computed<SessionClock | null>(() => (this.clockRead.hasValue() ? this.clockRead.value() : null));
  readonly clockProblem = computed(() => {
    const error: unknown = this.clockRead.error();
    return error === undefined ? null : extractServerMessage(error, 'The session times could not be read.');
  });

  /** The setup being edited. The times fill in from the clock the moment it is read, and only once. */
  readonly draft = linkedSignal<SessionClock | null, SetupDraft>({
    source: this.clock,
    computation: (clock, previous) => {
      if (previous === undefined) return newSetupDraft(clock);
      return clock === null ? previous.value : withDefaultTimes(previous.value, clock);
    },
  });
  readonly setupForm = form(this.draft, (path) => {
    disabled(path.noReentryClock, { when: ({ valueOf }) => !valueOf(path.noReentryOn) });
    disabled(path.perContractFee, { when: ({ valueOf }) => valueOf(path.feeKind) !== 'per_contract' });
    applyEach(path.legs, (leg) => {
      min(leg.contracts, 1);
      max(leg.contracts, () => this.limits()?.max_contracts_per_leg);
      min(leg.expiryDays, 0);
      max(leg.expiryDays, () => this.limits()?.max_expiry_days);
      disabled(leg.stopLossKind, { when: ({ valueOf }) => !valueOf(leg.stopLossOn) });
      disabled(leg.stopLossValue, { when: ({ valueOf }) => !valueOf(leg.stopLossOn) });
      disabled(leg.momentumKind, { when: ({ valueOf }) => !valueOf(leg.momentumOn) });
      disabled(leg.momentumValue, { when: ({ valueOf }) => !valueOf(leg.momentumOn) });
      disabled(leg.reentryKind, { when: ({ valueOf }) => !valueOf(leg.reentryOn) });
      disabled(leg.reentryCount, { when: ({ valueOf }) => !valueOf(leg.reentryOn) });
    });
  });
  readonly builder = signal(newLegBuilder());
  readonly builderForm = form(this.builder);

  /** The request body for the setup as it stands, or the problems that stop one. */
  private readonly built = computed(() => {
    const clock = this.clock();
    return clock === null ? null : buildSetup(this.draft(), clock, this.limits());
  });
  readonly body = computed(() => {
    const built = this.built();
    return built?.ok === true ? built.body : null;
  });

  private readonly checker = new SetupSummaryChecker((body) => this.service.describe(body), () => this.describeDebounceMs);
  readonly summary = this.checker.state;
  /** The setup as the server read it, once it has read the body held now: what Save, Export and a run send. */
  readonly readBack = computed(() => {
    const state = this.summary();
    const body = this.body();
    return state.kind === 'ready' && body !== null && JSON.stringify(state.body) === JSON.stringify(body) ? state.summary.setup : null;
  });

  /** Problems at their fields: the page's own first, then the server's. */
  readonly fieldProblems = computed<FieldProblems>(() => {
    const built = this.built();
    const state = this.summary();
    const own = built !== null && !built.ok ? built.problems : NO_PROBLEMS;
    const server = state.kind === 'invalid' ? state.problems.fields : NO_PROBLEMS;
    return own.size === 0 ? server : new Map([...server, ...own]);
  });
  readonly underlyingProblem = computed(() => this.fieldProblems().get(FIELD.underlying) ?? null);
  readonly squareOffProblem = computed(() => this.fieldProblems().get(FIELD.squareOff) ?? null);

  // The saved setups.
  private readonly library = resource({ loader: () => this.service.list() });
  readonly setups = computed<readonly SavedSetup[]>(() => (this.library.hasValue() ? this.library.value() : []));
  readonly libraryStatus = computed<LibraryStatus>(() => {
    if (this.library.error() !== undefined) return 'error';
    return this.library.hasValue() ? 'ready' : 'loading';
  });
  readonly libraryProblem = computed(() => {
    const error: unknown = this.library.error();
    return error === undefined ? null : extractServerMessage(error, 'The saved setups could not be read.');
  });
  /** The name of the setup that was opened or last saved, and the rule for the name a run is recorded under. */
  private readonly names = new RunNameTracker();
  readonly loadedName = this.names.name;
  /** The name a run is recorded under: the loaded or saved setup's name, but only while the setup on screen is still that one. */
  readonly runName = computed(() => this.names.nameFor(JSON.stringify(this.body())));

  /** What the address names, so the workspace can fail in place for a name that is not in the library. */
  readonly entry = computed<SetupEntry>(() => {
    const name = this.address();
    if (name === null) return { kind: 'draft' };
    // The draft already is this setup, whether or not the library has been read again since it was saved.
    if (this.names.name() === name) return { kind: 'open', item: this.setups().find((setup) => setup.name === name) ?? null };
    if (this.libraryStatus() === 'error') return { kind: 'failed', reason: this.libraryProblem() ?? 'The saved setups could not be read.' };
    if (this.libraryStatus() === 'loading') return { kind: 'loading' };
    const item = this.setups().find((setup) => setup.name === name);
    if (item === undefined) return { kind: 'unknown' };
    return item.setup == null ? { kind: 'cannot-open', reason: item.cannot_open_reason ?? 'The setup cannot be read.' } : { kind: 'open', item };
  });

  /** Whether the draft still matches the content last saved or opened under its name. */
  readonly standing = computed<DraftStanding>(() => {
    const name = this.names.name();
    if (name === null) return { kind: 'unsaved' };
    const example = this.setups().find((setup) => setup.name === name)?.example === true;
    return this.names.nameFor(JSON.stringify(this.body())) === null ? { kind: 'changed', name, example } : { kind: 'matches', name, example };
  });

  // Saving.
  readonly saveModel = signal({ name: '' });
  readonly automaticName = signal(true);
  private readonly generatedName = computed(() => generatedSetupName(this.draft()));
  readonly saveForm = form(this.saveModel);
  readonly saving = signal(false);
  private readonly lastSave = signal<{ readonly name: string; readonly stamp: string; readonly status: SaveStatus } | null>(null);
  private readonly stamp = computed(() => `${this.saveModel().name}\n${JSON.stringify(this.body())}`);
  readonly nameProblem = computed(
    () =>
      nameProblem(this.saveModel().name, this.limits()) ??
      (this.automaticName() && this.loadedName() !== this.saveModel().name && this.setups().some((setup) => setup.name === this.saveModel().name)
        ? 'A setup already uses this generated name. Choose a different name, or open the saved setup to edit it.'
        : null),
  );
  readonly canSave = computed(
    () =>
      this.readBack() !== null &&
      this.saveModel().name !== '' &&
      this.nameProblem() === null &&
      (!this.automaticName() || this.libraryStatus() === 'ready') &&
      !this.saving(),
  );
  /** What the last save said, until the name or the setup changes: then it no longer describes what is on screen. */
  readonly saveStatus = computed(() => {
    const last = this.lastSave();
    return last !== null && last.stamp === this.stamp() ? last.status : null;
  });
  readonly saveHint = computed(() => {
    if (this.saveModel().name === '') return 'Name the setup to save it.';
    if (this.automaticName() && this.libraryStatus() !== 'ready') return 'Load saved setups before saving a generated name, so an existing setup cannot be replaced accidentally.';
    return this.readBack() === null ? 'Save is available once the setup reads back cleanly.' : null;
  });

  // Import and export.
  readonly importNotice = signal<ImportNotice | null>(null);
  /** Counts what has put a setup into the form or begun to: a file chosen, a saved setup opened, a run's setup taken. An import that finds the count moved on is not the latest. */
  private setupReplacements = 0;
  readonly canExport = computed(() => this.readBack() !== null);
  /** What the setup waits for before it can be exported or run: to be complete, or to be read back by the server. */
  readonly waitingFor = computed<'complete' | 'read-back' | null>(() => {
    if (this.readBack() !== null) return null;
    const kind = this.summary().kind;
    return this.body() === null || kind === 'idle' || kind === 'invalid' ? 'complete' : 'read-back';
  });
  /** Why Export waits, until it can go: the setup is not complete, or the server has not read it back yet. */
  readonly exportWhy = computed(() => availableOnce('Export is', this.waitingFor()));

  constructor() {
    effect(() => {
      const name = this.address();
      const clock = this.clock();
      const setups = this.setups();
      const ready = this.libraryStatus() === 'ready';
      untracked(() => {
        if (name === this.acknowledged) return;
        if (name === null) {
          this.acknowledged = null;
          return;
        }
        if (clock === null || !ready) return;
        this.acknowledged = name;
        // The draft already is this setup when it was just saved under this name.
        if (this.names.name() === name) return;
        const item = setups.find((setup) => setup.name === name);
        if (item?.setup) this.open(item);
      });
    });
    effect(() => {
      const name = this.generatedName();
      if (this.automaticName()) untracked(() => this.saveModel.set({ name }));
    });
    effect(() => {
      const body = this.body();
      untracked(() => this.checker.show(body));
    });
  }

  /** The workspace was left: no read-back answer arrives after this. */
  stop(): void {
    this.checker.stop();
  }

  retryClock(): void {
    this.clockRead.reload();
  }

  retryLimits(): void {
    this.limitsRead.reload();
  }

  retryLibrary(): void {
    this.library.reload();
  }

  retrySummary(): void {
    this.checker.retry();
  }

  /** Opens a saved setup, or a shipped example, into the form. It takes the setup's name, so a run is recorded under it. */
  open(item: SavedSetup): void {
    const clock = this.clock();
    if (clock === null || item.setup == null) return;
    this.setupReplacements += 1;
    const opened = draftFromSetup(item.setup, clock);
    this.draft.set(opened);
    this.saveModel.set({ name: item.example ? '' : item.name });
    this.automaticName.set(item.example);
    this.names.set(item.name, bodyJsonOf(opened, clock));
    this.importNotice.set(null);
  }

  /**
   * A setup that has no saved name replaces the form: an imported file, or the setup of a past run.
   * The saved setup the address named is not touched, and the draft no longer claims to be it.
   */
  replaceWithUnnamed(setup: ServedSetup, notice: ImportNotice): void {
    const clock = this.clock();
    if (clock === null) return;
    this.setupReplacements += 1;
    this.draft.set(draftFromSetup(setup, clock));
    this.saveModel.set({ name: '' });
    this.automaticName.set(true);
    this.names.clear();
    this.importNotice.set(notice);
    this.home.set({ name: null });
  }

  addLeg(): void {
    if (builderProblems(this.builder(), this.limits()).size === 0) this.draft.update((draft) => withLegAdded(draft, this.builder(), this.limits()));
  }

  copyLeg(legId: string): void {
    this.draft.update((draft) => withLegCopied(draft, legId, this.limits()));
  }

  removeLeg(legId: string): void {
    this.draft.update((draft) => withLegRemoved(draft, legId));
  }

  async save(): Promise<void> {
    const body = this.readBack();
    const name = this.saveModel().name;
    if (body === null || !this.canSave()) return;
    const stamp = this.stamp();
    const savedBody = JSON.stringify(this.body());
    const replacedAtStart = this.names.replacements;
    this.saving.set(true);
    try {
      const saved = await this.service.save(name, body);
      this.lastSave.set({ name: saved.name, stamp, status: { kind: 'saved', text: `Saved “${saved.name}”.` } });
      // Another setup opened or imported while this was saving is what the form holds now, not this one.
      if (replacedAtStart === this.names.replacements) {
        this.names.set(saved.name, savedBody);
        this.home.set({ name: saved.name });
      }
      this.library.reload();
    } catch (error) {
      this.lastSave.set({ name, stamp, status: { kind: 'refused', text: reasonOf(error, 'The setup could not be saved. Try again.') } });
    } finally {
      this.saving.set(false);
    }
  }

  exportSetup(): void {
    const body = this.readBack();
    if (body !== null) this.download.save(setupFileBlob(body), setupFileName(this.saveModel().name, body.underlying));
  }

  async importFile(file: File): Promise<void> {
    const clock = this.clock();
    if (clock === null) return;
    // The server's reading of a file can take long, and the owner may choose another file or open a saved setup meanwhile: only the latest is applied.
    const mine = (this.setupReplacements += 1);
    const read = await readSetupFile(file);
    if (mine !== this.setupReplacements) return;
    if (!read.ok) {
      this.importNotice.set({ kind: 'refused', title: `${file.name} was not loaded.`, reasons: [read.message] });
      return;
    }
    let described: SetupSummary;
    try {
      described = await this.service.describe(read.json);
    } catch (error) {
      if (mine === this.setupReplacements) {
        this.importNotice.set({ kind: 'refused', title: `${file.name} was not loaded: it is not a setup the page can open.`, reasons: reasonsOf(error) });
      }
      return;
    }
    if (mine !== this.setupReplacements) return;
    // The form is made from the setup as the server read the file, so what the file left out is filled in by the server.
    this.replaceWithUnnamed(described.setup, { kind: 'loaded', title: `${file.name} was loaded.`, reasons: [] });
  }
}

/** The body the draft would make, as JSON; null while it makes none. */
function bodyJsonOf(draft: SetupDraft, clock: SessionClock): string | null {
  const built = buildSetup(draft, clock);
  return built.ok ? JSON.stringify(built.body) : null;
}

/** "Export is", then what it waits for. */
function availableOnce(subject: string, waiting: 'complete' | 'read-back' | null): string | null {
  if (waiting === null) return null;
  return `${subject} available once ${waiting === 'complete' ? 'the setup is complete' : 'the server has read the setup back'}.`;
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
