import { computed, DestroyRef, inject, Injectable, resource, signal, type Signal } from '@angular/core';

import { extractServerMessage } from '../../../components/broker/operation-error';
import { DataCheckFlow } from '../../../components/options-backtest/data-check-flow';
import { NO_DATES, rangeInstants, rangeOfStretch, type RangeChange } from '../../../components/options-backtest/duration';
import { OptionsBacktestService, SetupInvalidError } from '../../../components/options-backtest/options-backtest.service';
import type { ServedSetup, SetupSummary, TestableStretch } from '../../../components/options-backtest/options-backtest.types';
import type { GoldenOffer } from '../../../components/options-backtest/run/golden-offer';
import { RunLaunch } from '../../../components/options-backtest/run-launch';
import { RUN_POLL_MS } from '../../../components/options-backtest/run-poll';
import { describeDetail } from '../../../components/options-backtest/setup-problems';
import type { DateRange } from '../../../shared/date-picker';
import { OptionsSetupEvidenceStore } from '../../index/options-setup-evidence-store';
import { optionsSetupRoute } from '../../options-setups.address';
import { OptionsSetupWorkspaceStore } from '../options-setup-store';
import { reviewableRuns } from '../golden/golden-versions';

/** Said where a run cannot be offered to Golden for want of knowing what it froze. */
const GOLDEN_NEEDS_CONTENT = 'Taking a run to Golden needs to know which content the run froze, so it is not offered until that is read.';

/**
 * What one options setup's Explore keeps while the owner moves between its
 * views and tabs: the dates, the answer of Check data, the backtest on screen,
 * and the saved setup the backtest is made from.
 *
 * A backtest tests the setup as last saved, never the draft being edited in
 * Define. The saved setup is read back by the server before anything can be
 * checked or run, and what is sent is that read-back, so unsaved edits cannot
 * change what a run tested, and a run records the saved name it tested.
 * It lives with the workspace, as the Define draft does, so a tab switch
 * loses neither.
 */
@Injectable()
export class OptionsSetupExploreStore {
  private readonly setup = inject(OptionsSetupWorkspaceStore);
  private readonly service = inject(OptionsBacktestService);
  private readonly evidence = inject(OptionsSetupEvidenceStore);
  private readonly destroyRef = inject(DestroyRef);

  /** The saved name this Explore is for, or `null` for a draft with no name. */
  readonly name: Signal<string | null> = this.setup.address.asReadonly();

  /**
   * The setup as the library holds it under the name in the address. It is not the draft: an edit in Define changes it
   * only once saved. The same content read again after a save is the same setup, so nothing is asked again for it.
   */
  readonly saved = computed<ServedSetup | null>(
    () => {
      const entry = this.setup.entry();
      return entry.kind === 'open' ? (entry.item?.setup ?? null) : null;
    },
    { equal: (a, b) => JSON.stringify(a) === JSON.stringify(b) },
  );
  /** When the saved setup was saved; null for a shipped example, or while it is not read. */
  readonly savedAtMs = computed(() => {
    const entry = this.setup.entry();
    return entry.kind === 'open' ? (entry.item?.saved_at_ms ?? null) : null;
  });
  readonly example = computed(() => {
    const entry = this.setup.entry();
    return entry.kind === 'open' && entry.item?.example === true;
  });
  /** The address names a saved setup that the library has, but whose content is not yet in hand (a save is being read back). */
  readonly savedPending = computed(() => this.name() !== null && this.saved() === null);

  // The saved setup as the server reads it back: what a check and a run send.
  private readonly readBackRead = resource({
    params: () => this.saved() ?? undefined,
    loader: ({ params }) => this.service.describe(params),
  });
  readonly readBack = computed<SetupSummary | null>(() => (this.readBackRead.hasValue() ? this.readBackRead.value() : null));
  readonly readBackProblem = computed(() => {
    const error: unknown = this.readBackRead.error();
    if (error === undefined) return null;
    if (error instanceof SetupInvalidError) return error.details.map(describeDetail).join(' ');
    return extractServerMessage(error, 'The server could not read the saved setup back.');
  });
  private readonly setupToRun = computed(() => this.readBack()?.setup ?? null);

  /** Why Check data and Start wait for the saved setup, in words; null once it is read back. */
  readonly waitingForSetup = computed(() => {
    if (this.setupToRun() !== null) return null;
    if (this.name() === null) return 'Check data and Start are available once the setup is saved under a name.';
    return this.readBackProblem() === null
      ? 'Check data and Start are available once the server has read the saved setup back.'
      : 'Check data and Start are available once the saved setup reads back cleanly.';
  });

  // The dates of a run: kept apart from the setup, so a saved or exported setup never holds them.
  readonly range = signal<DateRange>(NO_DATES);
  private readonly instants = computed(() => rangeInstants(this.range()));
  /** Set when the page wrote the range on the owner's click (Run available dates), until the dates are edited. */
  readonly rangeNote = signal<RangeChange | null>(null);

  // Starting a run, and the run on screen.
  readonly launch = new RunLaunch(
    { startRun: (request) => this.service.startRun(request), runs: (query) => this.service.runs(query) },
    this.destroyRef,
    inject(RUN_POLL_MS),
  );
  private takenUp = false;

  // Check data.
  private readonly checkFlow = new DataCheckFlow((setup, startMs, endMs) => this.service.checkData(setup, startMs, endMs));
  readonly check = this.checkFlow.state;
  readonly pricesFromMs = this.checkFlow.pricesFromMs;
  readonly rangeProblem = computed(() => this.checkFlow.refusal() ?? this.launch.refusal());
  /** How many downloads on the Past chain view may have changed what is stored. */
  private readonly chainDownloads = signal(0);
  /** What a check would be asked with now, and a stamp to tell whether an answer still describes it: the saved setup, the dates, and how many backtests have started and chain downloads have come back since. */
  private readonly asking = computed(() => {
    const setup = this.setupToRun();
    const range = this.instants();
    return setup === null || range === null ? null : { setup, range, stamp: JSON.stringify([setup, range, this.launch.runsStarted(), this.chainDownloads()]) };
  });
  readonly checkIsStale = computed(() => {
    const state = this.check();
    return state.kind === 'ready' && state.asked.stamp !== this.asking()?.stamp;
  });
  readonly canCheck = computed(() => this.asking() !== null && this.check().kind !== 'checking');
  readonly canStart = computed(() => this.asking() !== null && !this.launch.busy());
  /** What Check data and Start wait for: the saved setup, then the dates; and that a backtest is going, for Start alone. */
  readonly runHint = computed(() => {
    const waiting = this.waitingForSetup();
    if (waiting !== null) return waiting;
    if (this.instants() === null) return 'Check data and Start are available once both dates are picked.';
    if (this.launch.starting()) return 'Starting the backtest…';
    return this.launch.busy() ? 'A backtest is already running.' : null;
  });

  /** Why a setup cannot be taken into the form yet: its times need the session clock. Null once the clock is read. */
  readonly setupBlockedWhy = computed(() =>
    this.setup.clock() === null ? 'A setup cannot be taken until the session times have been read. Define says where that stands.' : null,
  );

  /** On coming to Explore while a backtest is going: show it, so that no second one is started. Asked once. */
  takeUpRunInProgress(): void {
    if (this.takenUp) return;
    this.takenUp = true;
    void this.launch.takeUp();
  }

  rangeChanged(range: DateRange): void {
    this.range.set(range);
    this.rangeNote.set(null);
    this.checkFlow.clearRefusal();
    this.launch.clearRefusal();
  }

  /** The owner chose Run available dates: the range becomes the longest stretch that can be tested, and the page says so. */
  useStretch(stretch: TestableStretch): void {
    this.range.set(rangeOfStretch(stretch));
    this.rangeNote.set({ firstSessionOpenMs: stretch.first_session_open_ms, lastSessionOpenMs: stretch.last_session_open_ms });
    this.checkFlow.clearRefusal();
    this.launch.clearRefusal();
  }

  /** Asks which sessions of the range can be tested. Resolves once the answer is in, or the range is refused. */
  async checkData(): Promise<void> {
    const asking = this.asking();
    if (asking === null || !this.canCheck()) return;
    await this.checkFlow.run(asking.setup, asking.range, asking.stamp);
  }

  dismissCheck(): void {
    this.checkFlow.dismiss();
  }

  /** Starts the backtest with the saved setup as the server read it, the two instants, and the name it is saved under. */
  async start(): Promise<void> {
    const asking = this.asking();
    if (asking === null || !this.canStart()) return;
    await this.launch.start({ setup: asking.setup, start_ms: asking.range.start_ms, end_ms: asking.range.end_ms, setup_name: this.name() });
  }

  /** A download on the Past chain view came back: what is stored for an underlying and a date may have changed, so an answer of Check data is stale. */
  chainDownloaded(): void {
    this.chainDownloads.update((count) => count + 1);
  }

  /** A run's record said something new (it ended), or a run was deleted: the evidence is read again, so a run can be taken to Golden, or no longer is. */
  runChanged(): void {
    this.evidence.reloadIndex();
  }

  retryReadBack(): void {
    this.readBackRead.reload();
  }

  /**
   * What the run may be taken to Golden as. Golden reviews what the run froze, and lists only a completed run of the
   * saved content, so this offers exactly those: the run must be complete and have frozen this setup's saved content,
   * which is read from the evidence by exact content, never by name. Where it cannot be said, or it is another
   * content, or the run did not complete, the run is not offered, and why is said.
   */
  goldenOfferFor(runId: number): GoldenOffer {
    const name = this.name();
    if (name === null) return { kind: 'withheld', why: 'This draft has no saved name, so a run is not taken to Golden for it.' };
    const read = this.evidence.state();
    if (read.kind === 'failed') return { kind: 'withheld', why: `${GOLDEN_NEEDS_CONTENT} It could not be read.` };
    if (read.kind === 'loading') return { kind: 'withheld', why: `${GOLDEN_NEEDS_CONTENT} It is being read.` };
    const index = read.index;
    const hash = this.setup.savedHash();
    if (hash === null) return { kind: 'withheld', why: `${GOLDEN_NEEDS_CONTENT} The saved content of this setup is not in what was read.` };
    const run = index.runs.find((row) => row.id === runId);
    if (run === undefined) {
      return {
        kind: 'withheld',
        why: index.runs_total > index.runs.length
          ? `Run ${runId} is not among the newest ${index.runs.length} runs read, so which content it froze cannot be said here.`
          : `${GOLDEN_NEEDS_CONTENT} Run ${runId} is not in what was read yet.`,
      };
    }
    if (run.setup_sha256 === null) return { kind: 'withheld', why: `The setup of run ${runId} cannot be read, so it is not taken to Golden.` };
    if (run.setup_sha256 !== hash) {
      return { kind: 'withheld', why: `Run ${runId} froze other content than this setup’s saved content, so it is not taken to this setup’s Golden.` };
    }
    if (!reviewableRuns(index, hash).some((row) => row.id === runId)) {
      return { kind: 'withheld', why: `Run ${runId} is ${run.status}, not complete, so it is not taken to Golden: only a completed run can be approved.` };
    }
    return { kind: 'link', route: optionsSetupRoute(name, 'golden') };
  }

  /**
   * The owner chose Use this setup on a run: it opens in Define as a draft with no saved name. The setup this Explore
   * tests is the saved one and does not change; the draft becomes a setup of its own once it is saved.
   */
  useRunSetup(setup: ServedSetup): void {
    if (this.setup.clock() === null) return;
    this.setup.replaceWithUnnamed(setup, { kind: 'loaded', title: 'The setup of that run was loaded as a new draft. It is not saved under any name.', reasons: [] }, 'define');
  }
}
