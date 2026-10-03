import { ChangeDetectionStrategy, Component, ElementRef, afterRenderEffect, computed, input, signal, viewChild } from '@angular/core';
import { RouterLink } from '@angular/router';

import { LANE_MODE_WORDING } from '../../../../services/alpaca-live-verdict.service';
import { AlpacaLaneModeChipComponent } from '../../../brokers/alpaca-desk/alpaca-lane-mode-chip.component';
import { AssetIdentityComponent } from '../../../../shared/asset-identity';
import { AuthoredUsdPipe } from '../../../../shared/pipes/authored-usd.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import type { BotPanelView } from '../lib/broker-v2-panel.types';
import { keepInsideViewport } from '../strategy-view/popover-placement';

/** One holding as the header names it: "1 SPY". */
interface HeldFigure {
  readonly symbol: string;
  readonly quantity: number;
}

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
 * runs, when the market data feed last updated it.
 */
@Component({
  selector: 'app-bot-page-header',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AlpacaLaneModeChipComponent, AssetIdentityComponent, AuthoredUsdPipe, RouterLink, TimestampDisplayComponent],
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
