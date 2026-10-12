import { afterNextRender, ChangeDetectionStrategy, Component, computed, effect, inject, Injector, input, OnInit, resource, signal, untracked, viewChild } from '@angular/core';
import { form } from '@angular/forms/signals';

import { PastChainControlsComponent } from './past-chain-controls.component';
import { chosenStrikes, NO_CONTROLS, type ChainControls } from './past-chain-controls';
import { PastChainDownloadComponent } from './past-chain-download.component';
import { ChainDownloadFlow } from './past-chain-download-flow';
import { PastChainExpiriesComponent } from './past-chain-expiries.component';
import { readProblemOf, type ControlName, type ReadProblem } from './past-chain-problems';
import { PastChainTableComponent } from './past-chain-table.component';
import { chosenMinute, tradingDateMsOf } from './past-chain-time';
import { chainViewOf, dayViewOf, downloadPlan, downloadRequest, sessionNotOver, sessionOf } from './past-chain-view';
import { PastChainService } from './past-chain.service';

/**
 * The Past chain tab (#3030): pick an underlying and a past trading date, see every expiry the listing held that day,
 * open one and read its calls and puts with the price each last traded at and when, and ask the server to download
 * what is not stored. It owns every signal of the tab; the cards only show a slice and say what the owner meant.
 *
 * The trading date and the expiry are separate: nothing here writes one when the other changes. Every read is a
 * resource keyed on all that it was asked with, so an answer to an earlier date or expiry is never on screen, and the
 * chain is not read until the day has been read for the same underlying and date. The underlying starts as the
 * setup's, once, when the tab is first opened; from then on neither follows the other.
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

  protected readonly model = signal<ChainControls>(NO_CONTROLS);
  protected readonly controls = form(this.model);
  /** The `expiry_ms` of the expiry whose chain is open. Only the owner's pick writes it. */
  protected readonly expiryMs = signal<number | null>(null);
  /** Counts the downloads that have finished, so that reading again is a read of new params and never a reload. */
  private readonly generation = signal(0);

  private readonly underlying = computed(() => this.model().underlying);
  private readonly tradingDateMs = computed(() => tradingDateMsOf(this.model().date));

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
  protected readonly dayView = computed(() => dayViewOf(this.dayRead.status(), this.day(), this.dayProblem()));
  protected readonly session = computed(() => {
    const day = this.day();
    return day === null ? null : sessionOf(day);
  });

  // What the time and strikes boxes hold, as something that can be sent or as the reason it cannot.
  private readonly minute = computed(() => chosenMinute(this.model().time, this.tradingDateMs(), this.session()));
  private readonly strikes = computed(() => chosenStrikes(this.model().strikes));
  private readonly unsendable = computed<ControlName | null>(() => {
    if (this.minute().kind === 'refused') return 'time';
    return this.strikes().kind === 'refused' ? 'strikes' : null;
  });

  // One expiry: read only once the day is, and only with what can be sent.
  private readonly chainRead = resource({
    params: () => {
      const tradingDateMs = this.tradingDateMs();
      const expiryMs = this.expiryMs();
      const minute = this.minute();
      const strikes = this.strikes();
      if (this.day() === null || tradingDateMs === null || expiryMs === null || this.unsendable() !== null) return undefined;
      return {
        query: {
          underlying: this.underlying(),
          tradingDateMs,
          expiryMs,
          atMs: minute.kind === 'at' ? minute.atMs : null,
          strikes: strikes.kind === 'count' ? strikes.digits : null,
        },
        generation: this.generation(),
      };
    },
    loader: ({ params }) => this.service.expiry(params.query),
  });
  private readonly chain = computed(() => (this.chainRead.hasValue() ? this.chainRead.value() : null));
  private readonly chainProblem = computed(() => this.problemOf(this.chainRead.error(), 'The chain could not be read.'));
  protected readonly chainView = computed(() =>
    chainViewOf({
      dayReady: this.day() !== null,
      expiryPicked: this.expiryMs() !== null,
      blockedBy: this.unsendable(),
      status: this.chainRead.status(),
      chain: this.chain(),
      problem: this.chainProblem(),
    }),
  );
  /** What an empty strikes box means: the number the server used for the chain on screen. */
  protected readonly strikesShown = computed(() => this.chain()?.strikes_each_side ?? null);

  // A problem is said at the control it is about: the page's own where what was typed cannot be sent, else the server's.
  protected readonly timeProblem = computed(() => this.minuteProblem() ?? this.serverProblemAt('time'));
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
  /** The lake would not fetch this underlying's date: its session is not over. Said by the lake's answer, for this date only. */
  protected readonly notOver = computed(() => {
    const state = this.downloader.state();
    return state.kind === 'answered' && state.asked.underlying === this.underlying() && state.asked.tradingDateMs === this.tradingDateMs() && sessionNotOver(state.answer);
  });
  protected readonly plan = computed(() => (this.notOver() ? null : downloadPlan(this.day(), this.chain())));
  private readonly downloadCard = viewChild(PastChainDownloadComponent);

  constructor() {
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

  /** One call, naming exactly the contracts on screen that are not stored. When it has answered, the day and the chain are read again. */
  protected async startDownload(): Promise<void> {
    const plan = this.plan();
    const tradingDateMs = this.tradingDateMs();
    if (plan === null || tradingDateMs === null || this.downloader.busy()) return;
    const underlying = this.underlying();
    const request = downloadRequest(plan, underlying, tradingDateMs, this.expiryMs());
    const answered = await this.downloader.run({ underlying, tradingDateMs, request });
    if (answered) this.generation.update((count) => count + 1);
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
