import { afterNextRender, ChangeDetectionStrategy, Component, computed, inject, Injector, input, linkedSignal, viewChild } from '@angular/core';
import { Router, RouterLink } from '@angular/router';

import { DataCheckPanelComponent } from '../../../components/options-backtest/cards/data-check-panel.component';
import { DurationCardComponent } from '../../../components/options-backtest/cards/duration-card.component';
import { LaunchNoticeComponent } from '../../../components/options-backtest/cards/launch-notice.component';
import { RunActionsComponent } from '../../../components/options-backtest/cards/run-actions.component';
import { SetupSentencesComponent } from '../../../components/options-backtest/cards/setup-sentences.component';
import { HistoryTabComponent, type RunPair } from '../../../components/options-backtest/history/history-tab.component';
import { PastChainTabComponent } from '../../../components/options-backtest/past-chain/past-chain-tab.component';
import type { RunDetail } from '../../../components/options-backtest/options-backtest.types';
import { RunPanelComponent } from '../../../components/options-backtest/run/run-panel.component';
import { OptionsEvidenceViewComponent } from '../../../components/options-evidence/options-evidence-view.component';
import { OptionArchiveBannerComponent } from '../../../shared/option-archive/option-archive-banner.component';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import type { WorkspaceStateContent } from '../../../shared/workspace/workspace-state';
import { WorkspaceStateBlockComponent } from '../../../shared/workspace/workspace-state-block.component';
import {
  isOptionsExploreView,
  isRunView,
  OPTIONS_EXPLORE_DEFAULT_VIEW,
  OPTIONS_EXPLORE_VIEWS,
  optionsExploreQuery,
  optionsSetupRoute,
  type OptionsExploreView,
} from '../../options-setups.address';
import { OptionsSetupWorkspaceStore } from '../options-setup-store';
import { OptionsSetupExploreStore } from './options-setup-explore-store';

const WHOLE_NUMBER = /^[1-9][0-9]*$/;

/** A run id as an address writes it, or null when it names none that can be. */
function runIdOf(text: string | undefined): number | null {
  if (text === undefined || !WHOLE_NUMBER.test(text)) return null;
  const id = Number(text);
  return Number.isSafeInteger(id) ? id : null;
}

/**
 * Explore (PRD #3490 R22): what a saved options setup did over past dates.
 * Backtest runs the setup as last saved, over dates the owner picks; History
 * lists every run; Past chain reads a historical chain; and Performance,
 * Research lineage and Historical comparison read one run's retained evidence.
 *
 * The view and the run it is about are in the address, so an opened run has a
 * URL. A backtest tests the saved setup and not the draft in Define, so edits
 * that are not saved never change what a run tested, and a run keeps the
 * setup it froze. A complete run can be taken to Golden, which carries the run's
 * id and approves nothing.
 */
@Component({
  selector: 'app-options-setup-explore',
  imports: [
    RouterLink,
    OptionArchiveBannerComponent,
    WorkspaceStateBlockComponent,
    TimestampDisplayComponent,
    SetupSentencesComponent,
    DurationCardComponent,
    RunActionsComponent,
    DataCheckPanelComponent,
    LaunchNoticeComponent,
    RunPanelComponent,
    HistoryTabComponent,
    PastChainTabComponent,
    OptionsEvidenceViewComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-setup-explore.component.html',
  styleUrl: './options-setup-explore.component.scss',
  host: {
    // Shift+Enter starts a backtest from anywhere on the Backtest view, as the Start button says.
    '(document:keydown.shift.enter)': 'onStartShortcut($event)',
  },
})
export class OptionsSetupExploreComponent {
  readonly view = input<string>();
  readonly run = input<string>();
  readonly comparator_run_id = input<string>();

  protected readonly setup = inject(OptionsSetupWorkspaceStore);
  protected readonly explore = inject(OptionsSetupExploreStore);
  private readonly router = inject(Router);
  private readonly injector = inject(Injector);

  protected readonly views = OPTIONS_EXPLORE_VIEWS.tabs;
  protected readonly address = computed(() => optionsSetupRoute(this.explore.name(), 'explore'));

  protected readonly viewKnown = computed(() => this.view() === undefined || isOptionsExploreView(this.view() ?? ''));
  protected readonly activeView = computed<OptionsExploreView>(() => {
    const view = this.view() ?? '';
    return isOptionsExploreView(view) ? view : OPTIONS_EXPLORE_DEFAULT_VIEW;
  });
  protected readonly onRunView = computed(() => isRunView(this.activeView()));

  /** The run the address names. A run id that is not a positive whole number names none, and is said so. */
  protected readonly runId = computed(() => runIdOf(this.run()));
  protected readonly runNamedBadly = computed(() => this.run() !== undefined && this.run() !== '' && this.runId() === null);
  protected readonly comparatorId = computed(() => runIdOf(this.comparator_run_id()));
  /** The run last in view, which the links to the views about one run go on carrying while another view is shown. */
  private readonly focus = linkedSignal<number | null, number | null>({ source: this.runId, computation: (id, previous) => id ?? previous?.value ?? null });

  protected readonly viewLinks = computed(() =>
    this.views.map((view) => ({ id: view.id, label: view.label, query: optionsExploreQuery(view.id, isRunView(view.id) ? this.focus() : null) })),
  );
  protected readonly historyRun = computed(() => (this.activeView() === 'history' && this.comparison() === null ? this.runId() : null));
  protected readonly comparison = computed<RunPair | null>(() => {
    const a = this.runId();
    const b = this.comparatorId();
    return this.activeView() === 'history' && a !== null && b !== null && a !== b ? { a, b } : null;
  });
  protected readonly runQuery = computed(() => ({ run: String(this.runId() ?? '') }));

  /** Past chain is made when first opened and then kept, so leaving it and coming back finds the date, the expiry and the chain where they were. */
  protected readonly chainOpened = linkedSignal<OptionsExploreView, boolean>({
    source: this.activeView,
    computation: (view, previous) => view === 'chain' || (previous?.value ?? false),
  });
  protected readonly chainUnderlying = computed(() => this.explore.saved()?.underlying ?? this.setup.draft().underlying);

  /** Why Backtest has nothing to test, or what it is waiting for, in the domain's words. Absent: there is a saved setup. */
  protected readonly backtestState = computed<WorkspaceStateContent | null>(() => {
    if (this.explore.name() === null) {
      return {
        kind: 'idle',
        happened: 'This draft has no saved name, so there is no saved setup to test.',
        why: 'A backtest tests the setup as last saved, so that edits made later in Define can never change what a run tested.',
        next: 'Save the setup under a name in Define, then come back here.',
        actions: [{ id: 'define', label: 'Go to Define', route: optionsSetupRoute(null, 'define') }],
      };
    }
    if (this.explore.savedPending()) return { kind: 'checking', happened: 'Reading the saved setup.' };
    return null;
  });

  /** What the address asks for that Explore cannot show, said in place with a way to a view that exists. Absent: it can be shown. */
  protected readonly addressState = computed<WorkspaceStateContent | null>(() => {
    const history = { id: 'history', label: 'History', route: this.address(), queryParams: optionsExploreQuery('history') as Record<string, string> };
    if (!this.viewKnown()) {
      return {
        kind: 'unavailable',
        happened: `“${this.view()}” is not a view of Explore.`,
        why: 'Explore has Backtest, History, Past chain, Performance, Research lineage and Historical comparison.',
        next: 'Choose one of them.',
        actions: [{ id: 'backtest', label: 'Backtest', route: this.address() }],
      };
    }
    if (!this.onRunView()) return null;
    if (this.runNamedBadly()) {
      return {
        kind: 'invalid',
        happened: `“${this.run()}” is not a run id.`,
        why: 'A run is addressed by its id, a positive whole number.',
        next: 'Open a run in History.',
        actions: [history],
      };
    }
    if (this.runId() === null) {
      return {
        kind: 'idle',
        happened: 'No run is open.',
        why: 'Performance, research lineage and comparison are about one run.',
        next: 'Open a run in History, or start a backtest.',
        actions: [history],
      };
    }
    return null;
  });

  private readonly durationCard = viewChild(DurationCardComponent);
  private readonly checkPanel = viewChild(DataCheckPanelComponent);
  private readonly runPanel = viewChild(RunPanelComponent);

  constructor() {
    // Coming to Explore while a backtest is going: show it, so that no second one is started.
    this.explore.takeUpRunInProgress();
  }

  protected readonly offerFor = (id: number) => this.explore.goldenOfferFor(id);

  /** Asks which sessions can be tested; the answer lands away from the buttons that asked, so the keyboard goes to its heading. */
  protected checkData(): void {
    void this.explore.checkData().then(() => this.afterRender(() => this.checkPanel()?.focusHeading()));
  }

  /** The panel closes with the focused Dismiss in it, so the keyboard goes to the card of the dates it was about. */
  protected dismissCheck(): void {
    this.explore.dismissCheck();
    this.afterRender(() => this.durationCard()?.focusHeading());
  }

  /** Starts the backtest; the keyboard then goes to the run. */
  protected start(): void {
    void this.explore.start().then(() => this.afterRender(() => this.runPanel()?.focusHeading()));
  }

  /** Shift+Enter, from anywhere on the Backtest view but a field that uses it for a new line. A date typed and not yet applied is applied first. */
  protected onStartShortcut(event: Event): void {
    if (!(event instanceof KeyboardEvent) || this.activeView() !== 'backtest' || this.backtestState() !== null) return;
    if (event.repeat || event.isComposing || event.defaultPrevented || usesShiftEnter(event.target)) return;
    // A date applies when its field is left, and Shift+Enter is not the Enter that applies it.
    if (event.target instanceof HTMLInputElement && event.target.closest('app-date-range-picker') !== null) event.target.blur();
    if (!this.explore.canStart()) return;
    event.preventDefault();
    this.start();
  }

  /** The run on screen was read again and says something other than it was given: a finished run stops blocking a new start. */
  protected runUpdated(detail: RunDetail): void {
    this.explore.launch.update(detail);
    this.explore.runChanged();
  }

  /** History opened a run, or closed the one it had: the address says which. */
  protected runShown(id: number | null): void {
    void this.router.navigate(this.address(), { queryParams: optionsExploreQuery('history', id) });
  }

  protected comparisonShown(pair: RunPair | null): void {
    void this.router.navigate(this.address(), { queryParams: optionsExploreQuery('history', pair?.a ?? null, pair?.b ?? null) });
  }

  protected act(id: string): void {
    if (id === 'retry') this.explore.retryReadBack();
  }

  private afterRender(move: () => void): void {
    afterNextRender(move, { injector: this.injector });
  }
}

/** A field in which Shift+Enter means something of its own (a new line), so it is not Start. */
function usesShiftEnter(target: EventTarget | null): boolean {
  return target instanceof HTMLTextAreaElement || (target instanceof HTMLElement && target.isContentEditable);
}
