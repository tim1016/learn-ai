import { afterNextRender, ChangeDetectionStrategy, Component, computed, DestroyRef, effect, inject, Injector, input, linkedSignal, OnInit, resource, signal, untracked, viewChild } from '@angular/core';
import { debounce, form } from '@angular/forms/signals';

import { PastChainControlsComponent } from './past-chain-controls.component';
import { chosenStrikes, mostStrikesText, NO_CONTROLS, STRIKES_PAUSE_MS, type ChainControls } from './past-chain-controls';
import { PastChainDownloadComponent } from './past-chain-download.component';
import { ChainDownloadFlow } from './past-chain-download-flow';
import { PastChainExpiriesComponent } from './past-chain-expiries.component';
import { heldAnswer } from './past-chain-held';
import { readProblemOf, type ControlName, type ReadProblem } from './past-chain-problems';
import { PastChainTableComponent } from './past-chain-table.component';
import { chosenMinute, INCOMPLETE_TIME, tradingDateMsOf } from './past-chain-time';
import { chainViewOf, dayViewOf, downloadPlan, downloadRequest, sessionOf } from './past-chain-view';
import { PastChainService, sameExpiryQuery, type ExpiryQuery } from './past-chain.service';
import type { SetupLimits } from '../options-backtest.types';
import type { ChainDay, ChainExpiry } from './past-chain.types';

/**
 * The Past chain tab (#3030): pick an underlying and a past trading date, see every expiry the listing held that day,
 * open one and read its calls and puts with the price each last traded at and when, and ask the server to download
 * what is not stored. It owns every signal of the tab; the cards only show a slice and say what the owner meant.
 *
 * The trading date and the expiry are separate: nothing here writes one when the other changes. Every read is a
 * resource keyed on all that it was asked with, so an answer to an earlier date or expiry is never on screen, and the
 * chain is not read until the day has been read for the same underlying and date. The underlying starts as the
 * setup's, once, when the tab is first opened; from then on neither follows the other. Another underlying has other
 * expiries, so changing it puts the open expiry away. A read that is asked again after a download keeps the last
 * answer to the same question on screen meanwhile.
 */
@Component({
  selector: 'app-options-backtest-past-chain',
  imports: [PastChainControlsComponent, PastChainDownloadComponent, PastChainExpiriesComponent, PastChainTableComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './past-chain-tab.component.html',
  styleUrl: './past-chain-tab.component.scss',
})
export class PastChainTabComponent implements OnInit {
  private readonly service = inject(PastChainService);
  private readonly injector = inject(Injector);

  /** The setup's underlying at the moment the tab was first opened. Later changes of it are not followed. */
  readonly startingUnderlying = input.required<string>();
  /** What the server holds a read and a download to; null until the page has them, and the server then judges alone. */
  readonly limits = input<SetupLimits | null>(null);

  protected readonly model = signal<ChainControls>(NO_CONTROLS);
  // The strikes box is read once for what was typed: after a pause, when it is left, or on Enter.
  protected readonly controls = form(this.model, (path) => {
    debounce(path.strikes, STRIKES_PAUSE_MS);
  });
  /** Counts the downloads that may have changed what is stored, so that reading the day again is a read of new params and never a reload. */
  private readonly generation = signal(0);
  private destroyed = false;

  private readonly underlying = computed(() => this.model().underlying);
  private readonly tradingDateMs = computed(() => tradingDateMsOf(this.model().date));
  /** The `expiry_ms` of the expiry whose chain is open. Only the owner's pick writes it, and another underlying puts it away. */
  protected readonly expiryMs = linkedSignal<string, number | null>({ source: this.underlying, computation: () => null });

  // The day.
  private readonly dayRead = resource({
    params: () => {
      const underlying = this.underlying();
      const tradingDateMs = this.tradingDateMs();
      return underlying === '' || tradingDateMs === null ? undefined : { query: { underlying, tradingDateMs }, generation: this.generation() };
    },
    loader: ({ params }) => this.service.day(params.query),
  });
  private readonly day = computed(() => (this.dayRead.hasValue() ? this.dayRead.value() : null));
  private readonly dayProblem = computed(() => this.problemOf(this.dayRead.error(), 'The listing could not be read.'));
  // The listing of the same underlying and date stays on screen while a download's re-read is under way.
  private readonly dayHeld = heldAnswer<ChainDay>(
    () => {
      const tradingDateMs = this.tradingDateMs();
      return this.underlying() === '' || tradingDateMs === null ? null : `${this.underlying()}|${tradingDateMs}`;
    },
    () => this.dayRead.status(),
    () => this.day(),
  );
  private readonly shownDay = computed(() => this.dayHeld().answer);
  protected readonly dayAgain = computed(() => this.dayHeld().again);
  protected readonly dayView = computed(() => dayViewOf(this.dayRead.status(), this.shownDay(), this.dayProblem()));
  protected readonly session = computed(() => {
    const day = this.shownDay();
    return day === null ? null : sessionOf(day);
  });

  // What the time and strikes boxes hold, as something that can be sent or as the reason it cannot.
  private readonly typedMinute = computed(() => chosenMinute(this.model().time, this.tradingDateMs(), this.session()));
  // Half a time: the browser holds no value for it, and the form says so as a parse error.
  private readonly timeHalfTyped = computed(() => this.controls.time().errors().some((each) => each.kind === 'parse'));
  // Whether the box has ever held something, whole or half. `dirty()` is not that: the form marks a field dirty whenever it reads
  // the box, and a browser has it read the empty box when it is first drawn, so an untouched box would look emptied.
  private readonly timeEverHeld = linkedSignal<{ text: string; half: boolean }, boolean>({
    source: () => ({ text: this.model().time, half: this.timeHalfTyped() }),
    computation: (now, previous) => (previous?.value ?? false) || now.half || now.text.trim() !== '',
  });
  // Half a time or a box emptied after it held one is not the close: it is no time. A box that never held one is the close.
  private readonly timeIncomplete = computed(() => {
    // Read at every change, whatever half a time says, so that the record has seen each text the box held.
    const held = this.timeEverHeld();
    return this.timeHalfTyped() || (held && this.model().time.trim() === '');
  });
  // The last time that could be read: the chain keeps to it while the box holds none.
  private readonly lastValidTime = linkedSignal<{ text: string; usable: boolean }, string>({
    source: () => ({ text: this.model().time, usable: !this.timeIncomplete() && this.typedMinute().kind !== 'refused' }),
    computation: (now, previous) => (now.usable ? now.text : (previous?.value ?? '')),
  });
  private readonly minute = computed(() => {
    // Read at every change, so that the time it keeps is the last one the box held and not the first one it was asked for.
    const lastValid = this.lastValidTime();
    return this.timeIncomplete() ? chosenMinute(lastValid, this.tradingDateMs(), this.session()) : this.typedMinute();
  });
  /** The most strikes each side the server takes, once the page has its limits. */
  protected readonly mostStrikes = computed(() => mostStrikesText(this.limits()));
  private readonly strikes = computed(() => chosenStrikes(this.model().strikes, this.mostStrikes()));
  private readonly unsendable = computed<ControlName | null>(() => {
    if (this.minute().kind === 'refused') return 'time';
    return this.strikes().kind === 'refused' ? 'strikes' : null;
  });

  // One expiry: read only once the day is, and only with what can be sent.
  private readonly chainParams = computed<ExpiryQuery | undefined>(() => {
    const tradingDateMs = this.tradingDateMs();
    const expiryMs = this.expiryMs();
    const minute = this.minute();
    const strikes = this.strikes();
    if (this.day() === null || tradingDateMs === null || expiryMs === null || this.unsendable() !== null) return undefined;
    return {
      underlying: this.underlying(),
      tradingDateMs,
      expiryMs,
      atMs: minute.kind === 'at' ? minute.atMs : null,
      strikes: strikes.kind === 'count' ? strikes.digits : null,
    };
  }, { equal: sameExpiryQuery });
  // Unlike the day, no count rides in these params: a download that finishes reads the day again, and this read waits on the day.
  private readonly chainRead = resource({
    params: () => this.chainParams(),
    loader: ({ params }) => this.service.expiry(params),
  });
  private readonly chain = computed(() => (this.chainRead.hasValue() ? this.chainRead.value() : null));
  private readonly chainProblem = computed(() => this.problemOf(this.chainRead.error(), 'The chain could not be read.'));
  // What the chain is asked with, apart from the day it waits on: the same question keeps its last answer on screen while it is read again.
  private readonly chainHeld = heldAnswer<ChainExpiry>(
    () => {
      const tradingDateMs = this.tradingDateMs();
      const expiryMs = this.expiryMs();
      if (tradingDateMs === null || expiryMs === null || this.unsendable() !== null) return null;
      const minute = this.minute();
      const strikes = this.strikes();
      return `${this.underlying()}|${tradingDateMs}|${expiryMs}|${minute.kind === 'at' ? minute.atMs : 'close'}|${strikes.kind === 'count' ? strikes.digits : 'default'}`;
    },
    () => this.chainRead.status(),
    () => this.chain(),
  );
  private readonly shownChain = computed(() => this.chainHeld().answer);
  protected readonly chainAgain = computed(() => this.chainHeld().again);
  protected readonly chainView = computed(() =>
    chainViewOf({
      dayReady: this.shownDay() !== null,
      expiryPicked: this.expiryMs() !== null,
      blockedBy: this.unsendable(),
      status: this.chainRead.status(),
      chain: this.shownChain(),
      problem: this.chainProblem(),
    }),
  );
  /** What an empty strikes box means: the number the server used for the chain on screen. */
  protected readonly strikesShown = computed(() => this.shownChain()?.strikes_each_side ?? null);

  // A problem is said at the control it is about: the page's own where what was typed cannot be sent, else the server's.
  protected readonly timeProblem = computed(() => (this.timeIncomplete() ? INCOMPLETE_TIME : (this.minuteProblem() ?? this.serverProblemAt('time'))));
  protected readonly strikesProblem = computed(() => this.strikesRefusal() ?? this.serverProblemAt('strikes'));
  protected readonly underlyingProblem = computed(() => this.serverProblemAt('underlying'));
  protected readonly dateProblem = computed(() => this.serverProblemAt('date'));
  private readonly minuteProblem = computed(() => {
    const minute = this.minute();
    return minute.kind === 'refused' ? minute.problem : null;
  });
  private readonly strikesRefusal = computed(() => {
    const strikes = this.strikes();
    return strikes.kind === 'refused' ? strikes.problem : null;
  });

  // The download.
  private readonly downloader = new ChainDownloadFlow((request) => this.service.download(request));
  protected readonly downloadState = this.downloader.state;
  /** Why the lake will fetch nothing for this date yet, in its own words, as the day's answer says it. */
  protected readonly refusal = computed(() => this.shownDay()?.fetch_refusal ?? null);
  protected readonly plan = computed(() => downloadPlan(this.day(), this.chain(), this.limits()?.max_contracts_per_download ?? null));
  /** The download card offers to fetch what is missing, so the chain can point to it. */
  protected readonly downloadOffered = computed(() => this.plan() !== null);
  private readonly downloadCard = viewChild(PastChainDownloadComponent);

  constructor() {
    inject(DestroyRef).onDestroy(() => {
      this.destroyed = true;
    });
    // An answer says what it was asked for; a different underlying or date is a different subject, so the old one goes.
    effect(() => {
      this.underlying();
      this.tradingDateMs();
      untracked(() => this.downloader.clear());
    });
  }

  ngOnInit(): void {
    this.model.update((controls) => ({ ...controls, underlying: this.startingUnderlying() }));
  }

  protected pickExpiry(expiryMs: number): void {
    this.expiryMs.set(expiryMs);
  }

  protected retryDay(): void {
    this.dayRead.reload();
  }

  protected retryChain(): void {
    this.chainRead.reload();
  }

  /**
   * One call, naming exactly the contracts on screen that are not stored. When it has answered, or may have fetched
   * before it failed, the day and the chain are read again.
   */
  protected async startDownload(): Promise<void> {
    const plan = this.plan();
    const tradingDateMs = this.tradingDateMs();
    if (plan === null || tradingDateMs === null || this.downloader.busy()) return;
    const underlying = this.underlying();
    const request = downloadRequest(plan, underlying, tradingDateMs, this.expiryMs());
    const readAgain = await this.downloader.run({ underlying, tradingDateMs, request });
    // The tab may be gone by the time the server answers: there is nothing then to read again or to put the keyboard on.
    if (this.destroyed) return;
    if (readAgain) this.generation.update((count) => count + 1);
    afterNextRender(() => this.downloadCard()?.focusResult(), { injector: this.injector });
  }

  private problemOf(error: unknown, fallback: string): ReadProblem | null {
    return error === undefined ? null : readProblemOf(error, fallback);
  }

  /** The server's sentence for what is wrong with one control, from whichever read it refused. */
  private serverProblemAt(control: ControlName): string | null {
    const found = [this.dayProblem(), this.chainProblem()].find((problem) => problem?.control === control);
    return found?.message ?? null;
  }
}
