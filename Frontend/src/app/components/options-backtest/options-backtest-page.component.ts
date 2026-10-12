import { afterNextRender, ChangeDetectionStrategy, Component, computed, DestroyRef, effect, inject, Injector, input, linkedSignal, resource, signal, untracked, viewChild } from '@angular/core';
import { applyEach, disabled, form } from '@angular/forms/signals';
import { ButtonModule } from 'primeng/button';
import { Tab, TabList, TabPanel, TabPanels, Tabs } from 'primeng/tabs';

import type { DateRange } from '../../shared/date-picker';
import { FileDownloadService } from '../../shared/file/file-download.service';
import { PageHeaderComponent } from '../../shared/page-header/page-header.component';
import { extractServerMessage } from '../broker/operation-error';
import { BottomBarComponent, type SaveStatus } from './cards/bottom-bar.component';
import { CostsCardComponent } from './cards/costs-card.component';
import { DataCheckPanelComponent } from './cards/data-check-panel.component';
import { DurationCardComponent } from './cards/duration-card.component';
import { EntryCardComponent } from './cards/entry-card.component';
import { InstrumentCardComponent } from './cards/instrument-card.component';
import { LegBuilderCardComponent } from './cards/leg-builder-card.component';
import { LegsCardComponent } from './cards/legs-card.component';
import { LegwiseCardComponent } from './cards/legwise-card.component';
import { SideListComponent, type LibraryStatus } from './cards/side-list.component';
import { SummaryComponent } from './cards/summary.component';
import { HistoryTabComponent } from './history/history-tab.component';
import { RunPanelComponent } from './run/run-panel.component';
import { DataCheckFlow } from './data-check-flow';
import { NO_DATES, rangeInstants, rangeOfStretch, type RangeChange } from './duration';
import { builderProblems, newLegBuilder, withLegAdded, withLegCopied, withLegRemoved } from './leg-draft';
import { OptionsBacktestService, SetupInvalidError, SetupRefusedError } from './options-backtest.service';
import type { SavedSetup, ServedSetup, SessionClock, SetupSummary, TestableStretch } from './options-backtest.types';
import { RunLaunch } from './run-launch';
import { RUN_POLL_MS } from './run-poll';
import { buildSetup } from './setup-build';
import { draftFromSetup, newSetupDraft, withDefaultTimes, type SetupDraft } from './setup-draft';
import { readSetupFile, setupFileBlob, setupFileName } from './setup-file';
import { nameProblem } from './setup-limits';
import { describeDetail, FIELD, NO_PROBLEMS, type FieldProblems } from './setup-problems';
import { SetupSummaryChecker } from './setup-summary-checker';

export type OptionsBacktestTab = 'backtest' | 'history';

/** What an import said: it was loaded, or the reasons it was not. Nothing changes when it was not. */
interface ImportNotice {
  readonly kind: 'loaded' | 'refused';
  readonly title: string;
  readonly reasons: readonly string[];
}

/**
 * The Options Backtest page (#3030): build a setup, read it back in plain
 * words, save it, reopen it, delete it, export it to a file and import it from
 * one; pick the dates, check which sessions can be tested, start a backtest,
 * watch it, cancel it and read its result; and, on a second tab, reopen any
 * past run. The page owns every signal; the cards only show a slice of the
 * form and say what the owner meant.
 */
@Component({
  selector: 'app-options-backtest-page',
  imports: [
    ButtonModule,
    Tabs,
    TabList,
    Tab,
    TabPanels,
    TabPanel,
    PageHeaderComponent,
    SideListComponent,
    InstrumentCardComponent,
    EntryCardComponent,
    LegwiseCardComponent,
    LegBuilderCardComponent,
    LegsCardComponent,
    CostsCardComponent,
    DurationCardComponent,
    DataCheckPanelComponent,
    SummaryComponent,
    RunPanelComponent,
    HistoryTabComponent,
    BottomBarComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-backtest-page.component.html',
  styleUrl: './options-backtest-page.component.scss',
  host: {
    // Shift+Enter starts a backtest from anywhere on the page, as the bar's Start button says.
    '(document:keydown.shift.enter)': 'onStartShortcut($event)',
  },
})
export class OptionsBacktestPageComponent {
  private readonly service = inject(OptionsBacktestService);
  private readonly download = inject(FileDownloadService);
  private readonly injector = inject(Injector);

  /** The tab on show: building and running a backtest, or the runs made so far. */
  protected readonly activeTab = signal<OptionsBacktestTab>('backtest');

  /** How long the setup rests, in milliseconds, before it is read back. Specs set 0. */
  readonly describeDebounceMs = input(300);

  /** What the server holds a setup to. Until it is read, or when it cannot be, the page checks none of it and the server judges. */
  private readonly limitsRead = resource({ loader: () => this.service.limits() });
  protected readonly limits = computed(() => (this.limitsRead.hasValue() ? this.limitsRead.value() : null));
  protected readonly limitsProblem = computed(() => {
    const error: unknown = this.limitsRead.error();
    return error === undefined ? null : extractServerMessage(error, 'The server did not answer.');
  });

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
  private readonly legsCard = viewChild(LegsCardComponent);
  private readonly legBuilderCard = viewChild(LegBuilderCardComponent);
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
  /** The setup as the server read it, once it has read the body the page holds now: what Save, Export and a run send. */
  private readonly readBack = computed(() => {
    const state = this.summary();
    const body = this.body();
    return state.kind === 'ready' && body !== null && JSON.stringify(state.body) === JSON.stringify(body) ? state.summary.setup : null;
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
  protected readonly squareOffProblem = computed(() => this.fieldProblems().get(FIELD.squareOff) ?? null);

  // The dates of a run: kept apart from the draft, so a saved or exported setup never holds them.
  protected readonly range = signal<DateRange>(NO_DATES);
  private readonly instants = computed(() => rangeInstants(this.range()));
  /** Set when the page wrote the range on the owner's click (Run available dates), until the dates are edited. */
  protected readonly rangeNote = signal<RangeChange | null>(null);

  // Starting a run, and the run on screen.
  protected readonly launch = new RunLaunch({ startRun: (request) => this.service.startRun(request), runs: (query) => this.service.runs(query) }, inject(DestroyRef), inject(RUN_POLL_MS));
  private readonly runPanel = viewChild(RunPanelComponent);

  // Check data.
  private readonly checkFlow = new DataCheckFlow((setup, startMs, endMs) => this.service.checkData(setup, startMs, endMs));
  protected readonly check = this.checkFlow.state;
  protected readonly pricesFromMs = this.checkFlow.pricesFromMs;
  protected readonly rangeProblem = computed(() => this.checkFlow.refusal() ?? this.launch.refusal());
  /** What a check would be asked with now, and a stamp to tell whether an answer still describes it. */
  private readonly asking = computed(() => {
    const setup = this.readBack();
    const range = this.instants();
    return setup === null || range === null ? null : { setup, range, stamp: JSON.stringify([setup, range]) };
  });
  protected readonly checkIsStale = computed(() => {
    const state = this.check();
    return state.kind === 'ready' && state.asked.stamp !== this.asking()?.stamp;
  });
  protected readonly canCheck = computed(() => this.asking() !== null && this.check().kind !== 'checking');

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
  /** The body of the setup as it was when it was loaded or last saved under `loadedName`. */
  private readonly loadedBody = signal<string | null>(null);
  /** The name a run is recorded under: the loaded or saved setup's name, but only while the setup on screen is still that one. */
  private readonly runName = computed(() => {
    const name = this.loadedName();
    return name !== null && this.loadedBody() === JSON.stringify(this.body()) ? name : null;
  });
  protected readonly deleteProblem = signal<string | null>(null);

  // Saving.
  protected readonly saveModel = signal({ name: '' });
  protected readonly saveForm = form(this.saveModel);
  protected readonly saving = signal(false);
  private readonly lastSave = signal<{ readonly name: string; readonly stamp: string; readonly status: SaveStatus } | null>(null);
  /** Counts the times the form was replaced whole, by opening a setup or importing one, so a save that was under way can tell. */
  private formReplaced = 0;
  private readonly stamp = computed(() => `${this.saveModel().name}\n${JSON.stringify(this.body())}`);
  protected readonly nameProblem = computed(() => nameProblem(this.saveModel().name, this.limits()));
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
  /** What the setup waits for before it can be exported, checked or run: to be complete, or to be read back by the server. */
  private readonly waitingFor = computed<'complete' | 'read-back' | null>(() => {
    if (this.readBack() !== null) return null;
    const kind = this.summary().kind;
    return this.body() === null || kind === 'idle' || kind === 'invalid' ? 'complete' : 'read-back';
  });
  /** Why Export waits, until it can go: the setup is not complete, or the server has not read it back yet. */
  protected readonly exportWhy = computed(() => availableOnce('Export is', this.waitingFor()));
  /** What Check data and Start wait for: the setup, then the dates; and that a backtest is going, for Start alone. */
  protected readonly runHint = computed(() => {
    const waiting = availableOnce('Check data and Start are', this.waitingFor());
    if (waiting !== null) return waiting;
    if (this.instants() === null) return 'Check data and Start are available once both dates are picked.';
    if (this.launch.starting()) return 'Starting the backtest…';
    return this.launch.busy() ? 'A backtest is already running.' : null;
  });
  protected readonly canStart = computed(() => this.asking() !== null && !this.launch.busy());

  constructor() {
    effect(() => {
      const body = this.body();
      untracked(() => this.checker.show(body));
    });
    inject(DestroyRef).onDestroy(() => this.checker.stop());
    // Coming back to the page while a backtest is going: show it, so that no second one is started.
    void this.launch.takeUp();
  }

  protected rangeChanged(range: DateRange): void {
    this.range.set(range);
    this.rangeNote.set(null);
    this.checkFlow.clearRefusal();
    this.launch.clearRefusal();
  }

  protected checkData(): void {
    const asking = this.asking();
    if (asking !== null && this.canCheck()) void this.checkFlow.run(asking.setup, asking.range, asking.stamp);
  }

  protected dismissCheck(): void {
    this.checkFlow.dismiss();
  }

  /** The owner chose Run available dates: the range becomes the longest stretch that can be tested, and the page says so. */
  protected useStretch(stretch: TestableStretch): void {
    this.range.set(rangeOfStretch(stretch));
    this.rangeNote.set({ firstSessionOpenMs: stretch.first_session_open_ms, lastSessionOpenMs: stretch.last_session_open_ms });
    this.checkFlow.clearRefusal();
    this.launch.clearRefusal();
  }

  /** Starts the backtest with the setup as the server read it and the two instants; the keyboard then goes to the run. */
  protected start(): void {
    const asking = this.asking();
    if (asking === null || !this.canStart()) return;
    const request = { setup: asking.setup, start_ms: asking.range.start_ms, end_ms: asking.range.end_ms, setup_name: this.runName() };
    void this.launch.start(request).then(() => this.afterRender(() => this.runPanel()?.focusHeading()));
  }

  /** Shift+Enter, from anywhere on the page but a field that uses it for a new line. A date typed and not yet applied is applied first. */
  protected onStartShortcut(event: Event): void {
    if (!(event instanceof KeyboardEvent) || this.activeTab() !== 'backtest') return;
    if (event.repeat || event.isComposing || event.defaultPrevented || usesShiftEnter(event.target)) return;
    // A date applies when its field is left, and Shift+Enter is not the Enter that applies it.
    if (event.target instanceof HTMLInputElement && event.target.closest('app-date-range-picker') !== null) event.target.blur();
    if (!this.canStart()) return;
    event.preventDefault();
    this.start();
  }

  protected setTab(tab: string | number | undefined): void {
    if (tab === 'backtest' || tab === 'history') this.activeTab.set(tab);
  }

  /** The owner chose Use this setup on a run in History: the form takes that setup, under no name, and the Backtest tab is shown. */
  protected useRunSetup(setup: ServedSetup): void {
    const clock = this.clock();
    if (clock === null) return;
    this.draft.set(draftFromSetup(setup, clock));
    this.saveModel.set({ name: '' });
    this.loadedName.set(null);
    this.importNotice.set({ kind: 'loaded', title: 'The setup of that run was loaded. The dates were not changed.', reasons: [] });
    this.formReplaced += 1;
    this.activeTab.set('backtest');
  }

  protected retryClock(): void {
    this.clockRead.reload();
  }

  protected retryLimits(): void {
    this.limitsRead.reload();
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
    const opened = draftFromSetup(item.setup, clock);
    this.draft.set(opened);
    this.saveModel.set({ name: item.example ? '' : item.name });
    this.loadedName.set(item.name);
    this.loadedBody.set(bodyJsonOf(opened, clock));
    this.importNotice.set(null);
    this.formReplaced += 1;
  }

  protected async remove(name: string): Promise<void> {
    this.deleteProblem.set(null);
    try {
      await this.service.remove(name);
      // The setup is gone, so nothing may go on saying it was saved, or that the form holds it.
      if (this.loadedName() === name) this.loadedName.set(null);
      if (this.saveModel().name === name) this.saveModel.set({ name: '' });
      const last = this.lastSave();
      if (last?.status.kind === 'saved' && last.name === name) this.lastSave.set(null);
    } catch (error) {
      this.deleteProblem.set(reasonOf(error, 'The setup could not be deleted. Try again.'));
    }
    this.library.reload();
  }

  protected addLeg(): void {
    if (builderProblems(this.builder(), this.limits()).size === 0) this.draft.update((draft) => withLegAdded(draft, this.builder(), this.limits()));
  }

  /** The copy stands just after its leg; the keyboard goes to it, so the owner lands on what was made. */
  protected copyLeg(legId: string): void {
    const at = this.draft().legs.findIndex((leg) => leg.id === legId);
    this.draft.update((draft) => withLegCopied(draft, legId, this.limits()));
    if (at >= 0) this.afterRender(() => this.legsCard()?.focusHeading(at + 1));
  }

  /** The keyboard goes to where the deleted leg stood: the next leg's delete control, else the last one's, else Add leg. */
  protected removeLeg(legId: string): void {
    const at = this.draft().legs.findIndex((leg) => leg.id === legId);
    this.draft.update((draft) => withLegRemoved(draft, legId));
    const left = this.draft().legs.length;
    if (at < 0) return;
    this.afterRender(() => (left === 0 ? this.legBuilderCard()?.focusAdd() : this.legsCard()?.focusDelete(Math.min(at, left - 1))));
  }

  private afterRender(move: () => void): void {
    afterNextRender(move, { injector: this.injector });
  }

  protected async save(): Promise<void> {
    const body = this.readBack();
    const name = this.saveModel().name;
    if (body === null || !this.canSave()) return;
    const stamp = this.stamp();
    const savedBody = JSON.stringify(this.body());
    const replacedAtStart = this.formReplaced;
    this.saving.set(true);
    try {
      const saved = await this.service.save(name, body);
      this.lastSave.set({ name: saved.name, stamp, status: { kind: 'saved', text: `Saved “${saved.name}”.` } });
      // Another setup opened or imported while this was saving is what the form holds now, not this one.
      if (replacedAtStart === this.formReplaced) {
        this.loadedName.set(saved.name);
        this.loadedBody.set(savedBody);
      }
      this.library.reload();
    } catch (error) {
      this.lastSave.set({ name, stamp, status: { kind: 'refused', text: reasonOf(error, 'The setup could not be saved. Try again.') } });
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
    let described: SetupSummary;
    try {
      described = await this.service.describe(read.json);
    } catch (error) {
      this.importNotice.set({ kind: 'refused', title: `${file.name} was not loaded: it is not a setup the page can open.`, reasons: reasonsOf(error) });
      return;
    }
    // The form is made from the setup as the server read the file, so what the file left out is filled in by the server.
    this.draft.set(draftFromSetup(described.setup, clock));
    this.saveModel.set({ name: '' });
    this.loadedName.set(null);
    this.importNotice.set({ kind: 'loaded', title: `${file.name} was loaded.`, reasons: [] });
    this.formReplaced += 1;
  }
}

/** The body the draft would make, as JSON; null while it makes none. */
function bodyJsonOf(draft: SetupDraft, clock: SessionClock): string | null {
  const built = buildSetup(draft, clock);
  return built.ok ? JSON.stringify(built.body) : null;
}

/** A field in which Shift+Enter means something of its own (a new line), so it is not Start. */
function usesShiftEnter(target: EventTarget | null): boolean {
  return target instanceof HTMLTextAreaElement || (target instanceof HTMLElement && target.isContentEditable);
}

/** "Export is" or "Check data and Start are", then what they wait for. */
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
