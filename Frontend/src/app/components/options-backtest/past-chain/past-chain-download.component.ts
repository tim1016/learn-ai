import { ChangeDetectionStrategy, Component, computed, ElementRef, input, output, viewChild } from '@angular/core';
import { ButtonModule } from 'primeng/button';

import { AssetIdentityComponent } from '../../../shared/asset-identity';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import type { DownloadState } from './past-chain-download-flow';
import { outcomeCounts, planWords, type DownloadPlan } from './past-chain-view';
import type { DownloadItem } from './past-chain.types';

/**
 * "Download missing data": one button that says in words what it will fetch, busy while it runs, and then every item of
 * the answer with its outcome as the server gave it. A failed item names the lake's reason and the lake's own words;
 * nothing is hidden, and a failure that is only "being fetched by another request" is said to be that. When the lake
 * will fetch nothing for the date there is no plan, so there is nothing to offer here: the day's card says why.
 */
@Component({
  selector: 'app-options-backtest-past-chain-download',
  imports: [ButtonModule, AssetIdentityComponent, ReceiptLabelPipe, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './past-chain-download.component.html',
  styleUrls: ['../options-backtest-card.scss', './past-chain.scss', './past-chain-download.component.scss'],
})
export class PastChainDownloadComponent {
  /** What is missing on screen, or null when nothing is. */
  readonly plan = input<DownloadPlan | null>(null);
  readonly state = input.required<DownloadState>();
  readonly start = output();

  private readonly result = viewChild<ElementRef<HTMLElement>>('result');

  protected readonly words = computed(() => {
    const plan = this.plan();
    return plan === null ? '' : planWords(plan);
  });
  protected readonly busy = computed(() => this.state().kind === 'running');
  protected readonly counts = computed(() => {
    const state = this.state();
    return state.kind === 'answered' ? outcomeCounts(state.answer) : null;
  });
  /** Whether the card has anything to say: an offer, or a download going or answered. */
  protected readonly shown = computed(() => this.plan() !== null || this.state().kind !== 'idle');

  /** Puts the keyboard on the result, where the owner is when the button they pressed has gone busy or gone away. */
  focusResult(): void {
    this.result()?.nativeElement.focus();
  }

  /** An item is one thing the download was for, named by its kind and its subject. */
  protected itemKey(item: DownloadItem): string {
    return `${item.kind}:${item.subject}`;
  }

  protected kindLabel(item: DownloadItem): string {
    return item.kind === 'listing' ? 'Listing' : item.kind === 'underlying' ? "Underlying's bars" : 'Contract';
  }
}
