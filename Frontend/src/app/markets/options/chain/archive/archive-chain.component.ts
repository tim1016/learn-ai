import {
  afterNextRender,
  ChangeDetectionStrategy,
  Component,
  computed,
  DestroyRef,
  effect,
  inject,
  Injector,
  input,
  linkedSignal,
  output,
  resource,
  signal,
  untracked,
  viewChild,
} from '@angular/core';
import { debounce, form, FormField } from '@angular/forms/signals';
import { InputText } from 'primeng/inputtext';

import { ExpirationRibbonComponent } from '../../../../components/options-chain-v2/expiration-ribbon/expiration-ribbon.component';
import { OptionsBacktestService } from '../../../../components/options-backtest/options-backtest.service';
import { DateFieldComponent } from '../../../../shared/date-picker';
import { TimestampDisplayComponent } from '../../../../shared/timestamp';
import { WorkspaceStateBlockComponent } from '../../../../shared/workspace/workspace-state-block.component';
import { ChainViewComponent } from '../chain-view.component';
import { archiveBodyState, archiveChainState, archiveDayState, archivePresentation, expiryNote, refusalWords } from './archive-chain';
import { PastChainDownloadComponent } from './past-chain-download.component';
import { ChainDownloadFlow } from './past-chain-download-flow';
import { chosenStrikes, mostStrikesText, NO_STRIKES_TYPED, STRIKES_PAUSE_MS, type StrikesBox } from './past-chain-controls';
import { heldAnswer } from './past-chain-held';
import { readProblemOf, type ControlName, type ReadProblem } from './past-chain-problems';
import { checkedInstant, chosenMinute, clockTextOf, INCOMPLETE_TIME, sameTimeOnDate, tradingDateMsOf, type ChosenMinute } from './past-chain-time';
import { bodyOf, chainViewOf, dayViewOf, downloadPlan, downloadRequest, sessionOf } from './past-chain-view';
import { PastChainService, sameExpiryQuery, type ExpiryQuery } from './past-chain.service';
import type { ChainDay, ChainExpiry } from './past-chain.types';

/**
 * Past · Archive: what the data lake's archive held for the instrument on a past session, and
 * the price each contract last traded at by an instant of it (#3030, #3505).
 *
 * The session date, the instant within it and the expiry are the address's and arrive as inputs;
 * a pick is handed up and the address answers with new inputs. The date and the expiry are
 * separate: a new date leaves the open expiry as it was, and opening an expiry leaves the date.
 * Every read is a resource keyed on all that it was asked with, so an answer to an earlier date
 * or expiry is never on screen, and the chain is not read until the day has been read for the
 * same instrument and date. A read that is asked again after a download keeps the last answer
 * to the same question on screen meanwhile. Nothing here reads anything after the instant the
 * chain is asked at, and a download is only ever the operator's act.
 */
@Component({
  selector: 'app-archive-chain',
  imports: [
    FormField,
    InputText,
    DateFieldComponent,
    ExpirationRibbonComponent,
    PastChainDownloadComponent,
    ChainViewComponent,
    TimestampDisplayComponent,
    WorkspaceStateBlockComponent,
  ],
  templateUrl: './archive-chain.component.html',
  styleUrls: ['../chain-card.scss', './archive-chain.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ArchiveChainComponent {
  private readonly service = inject(PastChainService);
  private readonly limitsService = inject(OptionsBacktestService);
  private readonly injector = inject(Injector);

  readonly symbol = input.required<string>();
  /** The session date, `YYYY-MM-DD`; `null` until one is picked. */
  readonly date = input<string | null>(null);
  /** The instant within the session the chain is read at (`int64 ms UTC`); `null` reads the session's close. */
  readonly atMs = input<number | null>(null);
  readonly expiryMs = input<number | null>(null);

  /** A date the operator picked, with the same time of day carried onto it (`null` when none was set). */
  readonly datePicked = output<{ readonly date: string; readonly atMs: number | null }>();
  readonly timePicked = output<number | null>();
  readonly expiryPicked = output<number>();

  private readonly strikesTyped = signal<StrikesBox>(NO_STRIKES_TYPED);
  // The strikes box is read once for what was typed: after a pause, when it is left, or on Enter.
  protected readonly controls = form(this.strikesTyped, (path) => {
    debounce(path.strikes, STRIKES_PAUSE_MS);
  });
  /** Counts the downloads that may have changed what is stored, so that reading the day again is a read of new params and never a reload. */
  private readonly generation = signal(0);
  private destroyed = false;

  /** What the server holds a read and a download to; null until it has said, and the server then judges alone. */
  private readonly limitsRead = resource({ loader: () => this.limitsService.limits() });
  private readonly limits = computed(() => (this.limitsRead.hasValue() ? this.limitsRead.value() : null));

  private readonly underlying = computed(() => this.symbol().toUpperCase());
  private readonly tradingDateMs = computed(() => {
    const date = this.date();
    return date === null ? null : tradingDateMsOf(date);
  });

  // The day.
  private readonly dayRead = resource({
    params: () => {
      const tradingDateMs = this.tradingDateMs();
      return tradingDateMs === null ? undefined : { query: { underlying: this.underlying(), tradingDateMs }, generation: this.generation() };
    },
    loader: ({ params }) => this.service.day(params.query),
  });
  private readonly day = computed(() => (this.dayRead.hasValue() ? this.dayRead.value() : null));
  private readonly dayProblem = computed(() => this.problemOf(this.dayRead.error(), 'The listing could not be read.'));
  // The listing of the same instrument and date stays on screen while a download's re-read is under way.
  private readonly dayHeld = heldAnswer<ChainDay>(
    () => {
      const tradingDateMs = this.tradingDateMs();
      return tradingDateMs === null ? null : `${this.underlying()}|${tradingDateMs}`;
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
  /** The listing of the date when it holds something to say (expiries, or that it holds nothing a backtest can use). */
  protected readonly listing = computed(() => {
    const view = this.dayView();
    return view.kind === 'listed' || view.kind === 'empty' ? view.day : null;
  });
  protected readonly expiries = computed(() => this.listing()?.expiries.map((each) => each.expiry_ms) ?? []);
  protected readonly expiryNotes = computed(() => Object.fromEntries((this.listing()?.expiries ?? []).map((each) => [each.expiry_ms, expiryNote(each)])));
  protected readonly dayState = computed(() => archiveDayState(this.dayView()));
  /** Why the lake will fetch nothing for this date yet, in its own words, as the day's answer says it. */
  protected readonly refusal = computed(() => {
    const refusal = this.shownDay()?.fetch_refusal;
    return refusal == null ? null : refusalWords(refusal);
  });

  // The instant the chain is read at, against the session of its date once the day has said what the session is.
  private readonly minute = computed<ChosenMinute>(() => {
    const at = this.atMs();
    return at === null ? { kind: 'close' } : checkedInstant(at, this.session());
  });
  /** The time box's text: the clock time of the committed instant on its date, empty for the close. */
  protected readonly timeText = linkedSignal(() => {
    const at = this.atMs();
    const midnight = this.tradingDateMs();
    return at === null || midnight === null ? '' : clockTextOf(at, midnight);
  });
  /** A time the owner typed that was not committed: half a time, or one the session refuses. It goes when a time is committed or the date changes. */
  private readonly typedProblem = linkedSignal<string | null, string | null>({ source: () => `${this.date()}|${this.atMs()}`, computation: () => null });
  private readonly committedProblem = computed(() => {
    const minute = this.minute();
    return minute.kind === 'refused' ? minute.problem : null;
  });

  /** The most strikes each side the server takes, once the limits have arrived. */
  protected readonly mostStrikes = computed(() => mostStrikesText(this.limits()));
  private readonly strikes = computed(() => chosenStrikes(this.strikesTyped().strikes, this.mostStrikes()));
  private readonly unsendable = computed<ControlName | null>(() => {
    if (this.minute().kind === 'refused') return 'time';
    return this.strikes().kind === 'refused' ? 'strikes' : null;
  });

  // One expiry: read only once the day is, and only with what can be sent.
  private readonly chainParams = computed<ExpiryQuery | undefined>(
    () => {
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
    },
    { equal: sameExpiryQuery },
  );
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
  private readonly chainView = computed(() =>
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

  // The download.
  private readonly downloader = new ChainDownloadFlow((request) => this.service.download(request));
  protected readonly downloadState = this.downloader.state;
  protected readonly plan = computed(() => downloadPlan(this.day(), this.chain(), this.limits()?.max_contracts_per_download ?? null));
  private readonly downloadCard = viewChild(PastChainDownloadComponent);

  /** Why the chain card has no chain to draw, or `null` when it has one. */
  protected readonly chainState = computed(() => archiveChainState(this.chainView(), this.expiryMs()));
  /** The chain and the state its body is in (no strikes drawn, nothing listed), when the chain is read. */
  protected readonly shown = computed(() => {
    const view = this.chainView();
    if (view.kind !== 'shown') return null;
    const body = bodyOf(view.chain);
    return { presentation: archivePresentation(view.chain, body), state: archiveBodyState(view.chain, body, this.plan() !== null) };
  });

  // A problem is said at the control it is about: the page's own where what was typed cannot be sent, else the server's.
  protected readonly timeProblem = computed(() => this.typedProblem() ?? this.committedProblem() ?? this.serverProblemAt('time'));
  protected readonly strikesProblem = computed(() => this.strikesRefusal() ?? this.serverProblemAt('strikes'));
  protected readonly dateProblem = computed(() => this.serverProblemAt('date'));
  private readonly strikesRefusal = computed(() => {
    const strikes = this.strikes();
    return strikes.kind === 'refused' ? strikes.problem : null;
  });

  constructor() {
    inject(DestroyRef).onDestroy(() => {
      this.destroyed = true;
    });
    // An answer says what it was asked for; a different instrument or date is a different subject, so the old one goes.
    effect(() => {
      this.underlying();
      this.tradingDateMs();
      untracked(() => this.downloader.clear());
    });
  }

  protected onDate(date: string): void {
    // An empty date is an entry the field refused or half of one: its reason is at the field, and nothing is asked.
    if (date === '' || date === this.date()) return;
    const midnight = tradingDateMsOf(date);
    const at = this.atMs();
    const was = this.tradingDateMs();
    this.datePicked.emit({ date, atMs: at !== null && was !== null && midnight !== null ? sameTimeOnDate(at, was, midnight) : null });
  }

  /** Takes the time box when it is left: a whole time is committed, an emptied box is the close, half a time is not read. */
  protected onTime(event: Event): void {
    if (!(event.target instanceof HTMLInputElement)) return;
    const text = event.target.value;
    if (text === '' && event.target.validity.badInput) {
      this.typedProblem.set(INCOMPLETE_TIME);
      return;
    }
    if (text === '') {
      this.typedProblem.set(null);
      this.timePicked.emit(null);
      return;
    }
    const typed = chosenMinute(text, this.tradingDateMs(), this.session());
    if (typed.kind === 'refused') this.typedProblem.set(typed.problem);
    else if (typed.kind === 'at') {
      this.typedProblem.set(null);
      this.timePicked.emit(typed.atMs);
    }
  }

  /** Enter in the strikes box counts what is typed at once, as leaving the box does. */
  protected commitStrikes(): void {
    this.controls.strikes().markAsTouched();
  }

  protected onAction(id: string): void {
    if (id === 'retry-day') this.dayRead.reload();
    else if (id === 'retry-chain') this.chainRead.reload();
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
    // The page may be gone by the time the server answers: there is nothing then to read again or to put the keyboard on.
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
