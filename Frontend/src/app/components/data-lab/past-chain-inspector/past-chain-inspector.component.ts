/**
 * Past-chain inspector — D10 / R1 of the options-routes cleanup
 * (`docs/architecture/options-routes-research.md` § 5.1 R1).
 *
 * UX per UX-Q3 of `docs/architecture/options-ux-design-prompt.md`,
 * locked by the 2026-04-29 Claude Design pass:
 * - Inline collapsed card with a "Preview chain on this date" CTA
 * - Loading state with progress text + skeleton
 * - Expanded chain (calls left, puts right, ATM marker, change % colouring)
 * - Modal drill-down for per-contract minute bars
 * - "Show scan details" link off by default
 *
 * Lifted from the deleted /options-history component; the heavy
 * data-fetching analyze() body now lives in PastChainService.
 */
import {
  Component, ChangeDetectionStrategy, signal, computed, input, inject,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { firstValueFrom } from 'rxjs';
import { Button } from 'primeng/button';
import { Skeleton } from 'primeng/skeleton';
import { Dialog } from 'primeng/dialog';
import { Tooltip } from 'primeng/tooltip';
import { LineChartComponent } from '../../market-data/line-chart/line-chart.component';
import { VolumeChartComponent } from '../../market-data/volume-chart/volume-chart.component';
import { MarketDataService } from '../../../services/market-data.service';
import {
  PastChainService,
  PastChainContractRow,
  PastChainResult,
} from '../../../services/past-chain.service';
import { StockAggregate } from '../../../graphql/types';

type AtmMethod = 'open' | 'prevClose';

@Component({
  selector: 'app-past-chain-inspector',
  standalone: true,
  imports: [
    FormsModule,
    Button, Skeleton, Dialog, Tooltip,
    LineChartComponent, VolumeChartComponent,
  ],
  templateUrl: './past-chain-inspector.component.html',
  styleUrls: ['./past-chain-inspector.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class PastChainInspectorComponent {
  private pastChain = inject(PastChainService);
  private marketData = inject(MarketDataService);

  /** Inputs supplied by the host page (data-lab passes its current ticker + date). */
  ticker = input<string>('');
  analysisDate = input<string>('');

  /** Locally-controlled config — the host doesn't dictate these. */
  numStrikes = signal(5);
  atmMethod = signal<AtmMethod>('open');

  /** Three-state UX-Q3 lifecycle. */
  state = signal<'collapsed' | 'loading' | 'expanded'>('collapsed');
  loadingMessage = signal('');
  error = signal<string | null>(null);

  /** Materialised result. */
  result = signal<PastChainResult | null>(null);

  /** Audit table behind a "Show scan details" link, off by default. */
  showScanDetails = signal(false);

  /** Per-contract minute-detail modal. */
  detailModalOpen = signal(false);
  detailTicker = signal<string | null>(null);
  detailLoading = signal(false);
  detailBars = signal<StockAggregate[]>([]);

  /** Display helpers. */
  callRows = computed(() =>
    (this.result()?.contractRows ?? [])
      .filter(r => r.contractType === 'call')
      .sort((a, b) => a.strikePrice - b.strikePrice),
  );

  putRows = computed(() =>
    (this.result()?.contractRows ?? [])
      .filter(r => r.contractType === 'put')
      .sort((a, b) => a.strikePrice - b.strikePrice),
  );

  uniqueStrikes = computed(() => {
    const r = this.result();
    if (!r) return [] as number[];
    const set = new Set<number>();
    for (const row of r.contractRows) set.add(row.strikePrice);
    return [...set].sort((a, b) => a - b);
  });

  callByStrike = computed(() => {
    const m = new Map<number, PastChainContractRow>();
    for (const r of this.callRows()) m.set(r.strikePrice, r);
    return m;
  });

  putByStrike = computed(() => {
    const m = new Map<number, PastChainContractRow>();
    for (const r of this.putRows()) m.set(r.strikePrice, r);
    return m;
  });

  selectedScanCount = computed(() =>
    (this.result()?.scanResults ?? []).filter(r => r.selected).length,
  );

  /** Trigger the fetch from the collapsed state. */
  async preview(): Promise<void> {
    const ticker = this.ticker().trim().toUpperCase();
    const date = this.analysisDate();
    if (!ticker) {
      this.error.set('Pick a ticker before previewing the chain.');
      return;
    }
    if (!date) {
      this.error.set('Pick an analysis date before previewing the chain.');
      return;
    }

    this.state.set('loading');
    this.loadingMessage.set(`Scanning candidate strikes for ${ticker} on ${date}…`);
    this.error.set(null);
    this.result.set(null);

    try {
      const result = await this.pastChain.fetchPastChain({
        ticker,
        date,
        numStrikes: this.numStrikes(),
        atmMethod: this.atmMethod(),
      });
      this.result.set(result);
      this.state.set('expanded');
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
      this.state.set('collapsed');
    } finally {
      this.loadingMessage.set('');
    }
  }

  collapse(): void {
    this.state.set('collapsed');
    this.result.set(null);
    this.showScanDetails.set(false);
  }

  toggleScanDetails(): void {
    this.showScanDetails.update(v => !v);
  }

  /** Open the per-contract minute-detail modal (UX-Q3: modal, not nested drawer). */
  async openDetail(optionTicker: string): Promise<void> {
    this.detailTicker.set(optionTicker);
    this.detailModalOpen.set(true);
    this.detailLoading.set(true);
    this.detailBars.set([]);

    try {
      const date = this.analysisDate();
      const r = await firstValueFrom(
        this.marketData.getOrFetchStockAggregates(optionTicker, date, date, 'minute', 1),
      );
      this.detailBars.set(r.aggregates ?? []);
    } catch {
      // Non-fatal.
    } finally {
      this.detailLoading.set(false);
    }
  }

  closeDetail(): void {
    this.detailModalOpen.set(false);
    this.detailTicker.set(null);
    this.detailBars.set([]);
  }

  /** Compatibility with PrimeNG Dialog two-way visibility. */
  onDetailVisibleChange(visible: boolean): void {
    if (!visible) this.closeDetail();
  }

  isAtm(strike: number): boolean {
    return strike === (this.result()?.atmStrike ?? -1);
  }

  formatPrice(val: number | null | undefined): string {
    return val != null ? val.toFixed(2) : '—';
  }

  formatChange(val: number | null): string {
    if (val == null) return '—';
    const sign = val >= 0 ? '+' : '';
    return `${sign}${val.toFixed(2)}`;
  }

  formatChangePct(val: number | null): string {
    if (val == null) return '—';
    const sign = val >= 0 ? '+' : '';
    return `${sign}${val.toFixed(1)}%`;
  }

  formatVolume(val: number | null | undefined): string {
    if (val == null) return '—';
    return val.toLocaleString();
  }
}
