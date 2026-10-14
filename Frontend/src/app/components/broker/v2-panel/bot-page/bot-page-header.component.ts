import { ChangeDetectionStrategy, Component, ElementRef, afterRenderEffect, computed, input, signal, viewChild } from '@angular/core';
import { RouterLink } from '@angular/router';

import { LANE_MODE_WORDING } from '../../../../services/alpaca-live-verdict.service';
import { AlpacaLaneModeChipComponent } from '../../../brokers/alpaca-desk/alpaca-lane-mode-chip.component';
import { AssetIdentityComponent } from '../../../../shared/asset-identity';
import { BotInstrumentComponent } from '../../../../shared/bot-instrument/bot-instrument.component';
import { AuthoredUsdPipe } from '../../../../shared/pipes/authored-usd.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import type { BotPanelView, MarketPulseView } from '../lib/broker-v2-panel.types';
import { keepInsideViewport } from '../strategy-view/popover-placement';

/** One holding as the header names it: "1 SPY". */
interface HeldFigure {
  readonly symbol: string;
  readonly quantity: number;
}

type FeedState = MarketPulseView['feed_state'];

/** The feed line's word for each state the panel reports: only `LIVE` may read "live" (#3164). */
const FEED_WORDING: Readonly<Record<FeedState, string>> = {
  LIVE: 'live',
  IDLE: 'idle',
  STALE: 'stale',
  MISSING: 'missing',
};

/** Worst first: the line follows whichever of the bot's feeds is doing worst. */
const FEED_WORST_FIRST: readonly FeedState[] = ['MISSING', 'STALE', 'IDLE', 'LIVE'];

let nextSummaryId = 0;

/**
 * The bot page's banner (#2794 R1, R3, R9), drawn in the account
 * workspace's header beside Deploy a bot: the way back, the strategy and
 * symbol over the bot's name, the one status the bot owns, and its key
 * figures. It carries no LIVE chip -- the top bar and account strip say that
 * -- no account-scoped verdict and no ticking time. A Dry Run is still marked
 * as simulated cash: its money is not the account's (H23).
 *
 * The status opens the backend's one-line summary of the run and, while it
 * runs, the state of the market data feed and when its last bar arrived.
 */
@Component({
  selector: 'app-bot-page-header',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AlpacaLaneModeChipComponent, AssetIdentityComponent, AuthoredUsdPipe, BotInstrumentComponent, RouterLink,
    TimestampDisplayComponent,
  ],
  templateUrl: './bot-page-header.component.html',
  styleUrl: './bot-page-header.component.scss',
  host: {
    '(document:mousedown)': 'onDocumentMousedown($event)',
    '(keydown.escape)': 'summaryOpen.set(false)',
  },
})
export class BotPageHeaderComponent {
  readonly panel = input.required<BotPanelView>();
  readonly backRoute = input.required<readonly string[]>();
  readonly backLabel = input.required<string>();

  protected readonly page = computed(() => this.panel().bot_page ?? null);
  protected readonly held = computed((): readonly HeldFigure[] =>
    Object.entries(this.panel().exposure)
      .map(([symbol, quantity]) => ({ symbol, quantity }))
      .sort((left, right) => left.symbol.localeCompare(right.symbol)),
  );
  /** Of the feed the bot listens to and the one of the asset it trades (#2912), the one doing worst. */
  protected readonly feed = computed((): MarketPulseView => {
    const { market_pulse, traded_market_pulse } = this.panel();
    const pulses = traded_market_pulse ? [market_pulse, traded_market_pulse] : [market_pulse];
    return pulses.reduce((worst, next) =>
      FEED_WORST_FIRST.indexOf(next.feed_state) < FEED_WORST_FIRST.indexOf(worst.feed_state) ? next : worst,
    );
  });
  protected readonly feedWord = computed(() => FEED_WORDING[this.feed().feed_state]);
  protected readonly dryRunChip = { tone: 'dry_run', mode: LANE_MODE_WORDING.dry_run } as const;
  /** Screen-reader word of each new panel revision, with no visual time ticking. */
  protected readonly snapshotStatus = computed(
    () => `Revision ${this.panel().revision}${this.panel().health.running ? ' running' : ' stopped'}`,
  );

  protected readonly summaryOpen = signal(false);
  protected readonly summaryId = `bot-run-summary-${nextSummaryId++}`;
  private readonly statusAnchor = viewChild<ElementRef<HTMLElement>>('statusAnchor');
  private readonly summary = viewChild<ElementRef<HTMLElement>>('summary');

  constructor() {
    // On a phone the status sits mid-row, and its summary would run off the right edge.
    afterRenderEffect(() => {
      const summary = this.summary();
      if (summary !== undefined) keepInsideViewport(summary.nativeElement);
    });
  }

  /** A press anywhere outside the status closes its summary; mousedown, so one press cannot both close it and toggle it back open. */
  protected onDocumentMousedown(event: MouseEvent): void {
    if (!this.summaryOpen()) return;
    const anchor = this.statusAnchor()?.nativeElement;
    if (event.target instanceof Node && anchor?.contains(event.target)) return;
    this.summaryOpen.set(false);
  }
}
