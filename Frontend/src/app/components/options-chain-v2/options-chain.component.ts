import {
  Component, inject, signal, computed,
  ChangeDetectionStrategy, OnInit, OnDestroy,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { DecimalPipe } from '@angular/common';
import { firstValueFrom } from 'rxjs';
import { Drawer } from 'primeng/drawer';
import { InputText } from 'primeng/inputtext';
import { Button } from 'primeng/button';
import { Tooltip } from 'primeng/tooltip';
import { Skeleton } from 'primeng/skeleton';
import { ProgressSpinner } from 'primeng/progressspinner';
import { MarketDataService } from '../../services/market-data.service';
import {
  SnapshotUnderlyingResult, SnapshotContractResult, StockAggregate,
  StockTickerSnapshot,
} from '../../graphql/types';
import { getMinAllowedDate } from '../../utils/date-validation';
import { CandlestickChartComponent } from '../market-data/candlestick-chart/candlestick-chart.component';
import { VolumeChartComponent } from '../market-data/volume-chart/volume-chart.component';
import { ExpirationRibbonComponent } from './expiration-ribbon/expiration-ribbon.component';

// Pre-computed row with all display data — no method calls needed in template
interface ChainRow {
  strike: number;
  strikeFormatted: string;
  call: SnapshotContractResult | null;
  put: SnapshotContractResult | null;
  isAtm: boolean;
  itmCall: boolean;
  itmPut: boolean;
  otmCall: boolean;
  otmPut: boolean;
  ivFormatted: string;
  // Pre-formatted call data
  callVega: string;
  callTheta: string;
  callGamma: string;
  callDelta: string;
  callPrice: string;
  callBidAsk: string;  // "B/A" line below price
  callOi: string;
  callVolume: string;
  callVolumeBarWidth: number;
  // Pre-formatted put data
  putVega: string;
  putTheta: string;
  putGamma: string;
  putDelta: string;
  putPrice: string;
  putBidAsk: string;
  putOi: string;
  putVolume: string;
  putVolumeBarWidth: number;
}

interface SelectedContract {
  ticker: string;
  contractType: string;
  strikePrice: number;
  expirationDate: string;
  snapshot: SnapshotContractResult;
}

interface ParsedOptionTicker {
  underlying: string;
  expDate: string;        // e.g. "Feb 23, 2026"
  expDateShort: string;   // e.g. "02/23/26"
  type: string;           // "Call" or "Put"
  strike: string;         // e.g. "$690.00"
}

@Component({
  selector: 'app-options-chain',
  standalone: true,
  imports: [
    FormsModule, DecimalPipe,
    Drawer, InputText, Button,
    Tooltip, Skeleton, ProgressSpinner,
    CandlestickChartComponent, VolumeChartComponent,
    ExpirationRibbonComponent,
  ],
  templateUrl: './options-chain.component.html',
  styleUrls: ['./options-chain.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class OptionsChainComponent implements OnInit, OnDestroy {
  private marketDataService = inject(MarketDataService);

  // Input state
  ticker = signal('SPY');

  // Expiration ribbon state
  availableExpirations = signal<string[]>([]);
  selectedExpiration = signal<string | null>(null);
  expirationsLoading = signal(false);

  // Chain data state
  underlying = signal<SnapshotUnderlyingResult | null>(null);
  allContracts = signal<SnapshotContractResult[]>([]);
  chainLoading = signal(false);
  error = signal<string | null>(null);

  // Stock snapshot for real-time price info
  stockSnapshot = signal<StockTickerSnapshot | null>(null);

  // Help sidebar
  helpVisible = signal(false);

  // Strike controls
  strikeSortDirection = signal<'asc' | 'desc'>('asc');
  strikeRange = signal(15);
  strikeRangeOptions = [5, 10, 15, 20, 25, 30, 40, 50];
  showAllStrikes = signal(false);

  // Overlay state
  overlayVisible = signal(false);
  selectedContract = signal<SelectedContract | null>(null);
  overlayLoading = signal(false);
  overlayAggregates = signal<StockAggregate[]>([]);
  overlayError = signal<string | null>(null);

  // Single computed that does ALL the work: ATM, grouping, sorting, slicing, formatting
  visibleRows = computed<ChainRow[]>(() => {
    const contracts = this.allContracts();
    const price = this.underlying()?.price ?? 0;
    const dir = this.strikeSortDirection();
    const range = this.strikeRange();
    const showAll = this.showAllStrikes();

    if (contracts.length === 0) return [];

    // Build call/put maps and collect strikes in one pass
    const callMap = new Map<number, SnapshotContractResult>();
    const putMap = new Map<number, SnapshotContractResult>();
    const strikeSet = new Set<number>();

    for (const c of contracts) {
      if (c.strikePrice == null) continue;
      strikeSet.add(c.strikePrice);
      if (c.contractType === 'call') callMap.set(c.strikePrice, c);
      else if (c.contractType === 'put') putMap.set(c.strikePrice, c);
    }

    // Sort strikes
    const strikes = [...strikeSet].sort((a, b) =>
      dir === 'asc' ? a - b : b - a
    );

    // Find ATM
    let atmStrike: number | null = null;
    if (price > 0 && strikes.length > 0) {
      let minDist = Infinity;
      for (const s of strikes) {
        const dist = Math.abs(s - price);
        if (dist < minDist) { atmStrike = s; minDist = dist; }
      }
    }

    // Slice around ATM (unless "Show All" is active)
    let visibleStrikes = strikes;
    if (!showAll && atmStrike != null) {
      const atmIdx = strikes.indexOf(atmStrike);
      if (atmIdx !== -1) {
        const start = Math.max(0, atmIdx - range);
        const end = Math.min(strikes.length, atmIdx + range + 1);
        visibleStrikes = strikes.slice(start, end);
      }
    }

    // Compute max volumes for bar width scaling
    let maxCallVol = 0;
    let maxPutVol = 0;
    for (const s of visibleStrikes) {
      const cv = callMap.get(s)?.day?.volume ?? 0;
      const pv = putMap.get(s)?.day?.volume ?? 0;
      if (cv > maxCallVol) maxCallVol = cv;
      if (pv > maxPutVol) maxPutVol = pv;
    }

    // Build fully pre-computed rows
    return visibleStrikes.map(strike => {
      const call = callMap.get(strike) ?? null;
      const put = putMap.get(strike) ?? null;
      const isAtm = strike === atmStrike;

      return {
        strike,
        strikeFormatted: strike.toFixed(2),
        call,
        put,
        isAtm,
        itmCall: price > 0 && strike < price && strike !== atmStrike,
        itmPut: price > 0 && strike > price && strike !== atmStrike,
        otmCall: price > 0 && strike > price && strike !== atmStrike,
        otmPut: price > 0 && strike < price && strike !== atmStrike,
        ivFormatted: this.fmtIv(call?.impliedVolatility ?? put?.impliedVolatility ?? null),
        callVega: this.fmtGreek(call?.greeks?.vega ?? null),
        callTheta: this.fmtGreek(call?.greeks?.theta ?? null),
        callGamma: this.fmtGreek(call?.greeks?.gamma ?? null),
        callDelta: this.fmtGreek(call?.greeks?.delta ?? null),
        callPrice: this.resolvePrice(call),
        callBidAsk: this.fmtBidAsk(call),
        callOi: this.fmtNum(call?.openInterest ?? null),
        callVolume: this.fmtNum(call?.day?.volume ?? null),
        callVolumeBarWidth: this.barWidth(call?.day?.volume ?? null, maxCallVol),
        putVega: this.fmtGreek(put?.greeks?.vega ?? null),
        putTheta: this.fmtGreek(put?.greeks?.theta ?? null),
        putGamma: this.fmtGreek(put?.greeks?.gamma ?? null),
        putDelta: this.fmtGreek(put?.greeks?.delta ?? null),
        putPrice: this.resolvePrice(put),
        putBidAsk: this.fmtBidAsk(put),
        putOi: this.fmtNum(put?.openInterest ?? null),
        putVolume: this.fmtNum(put?.day?.volume ?? null),
        putVolumeBarWidth: this.barWidth(put?.day?.volume ?? null, maxPutVol),
      };
    });
  });

  // Computed: overlay summary stats
  overlaySummary = computed(() => {
    const aggs = this.overlayAggregates();
    if (aggs.length === 0) return null;
    let high = -Infinity, low = Infinity, volSum = 0;
    for (const a of aggs) {
      if (a.high > high) high = a.high;
      if (a.low < low) low = a.low;
      volSum += a.volume;
    }
    return {
      high,
      low,
      avgVolume: Math.round(volSum / aggs.length),
      totalBars: aggs.length,
    };
  });

  // Computed: parsed option ticker for drawer header
  parsedTicker = computed<ParsedOptionTicker | null>(() => {
    const sc = this.selectedContract();
    if (!sc) return null;
    return this.parseOptionTicker(sc.ticker);
  });

  // Computed: break-even price for overlay
  breakEven = computed<number | null>(() => {
    const sc = this.selectedContract();
    if (!sc) return null;
    const price = parseFloat(this.resolvePrice(sc.snapshot));
    if (isNaN(price)) return null;
    return sc.contractType === 'call'
      ? sc.strikePrice + price
      : sc.strikePrice - price;
  });

  // Skeleton placeholders
  skeletonRows = Array.from({ length: 8 }, (_, i) => i);
  skeletonCols = Array.from({ length: 16 }, (_, i) => i);

  ngOnInit(): void {
    document.documentElement.classList.add('app-dark');
  }

  ngOnDestroy(): void {
    document.documentElement.classList.remove('app-dark');
  }

  async fetchExpirations(): Promise<void> {
    const t = this.ticker().trim().toUpperCase();
    if (!t) return;

    this.expirationsLoading.set(true);
    this.error.set(null);
    this.availableExpirations.set([]);
    this.selectedExpiration.set(null);
    this.underlying.set(null);
    this.allContracts.set([]);
    this.stockSnapshot.set(null);

    try {
      // Fetch expirations and stock snapshot in parallel
      const [expirations, snapshotResult] = await Promise.all([
        firstValueFrom(this.marketDataService.getOptionsExpirations(t)),
        firstValueFrom(this.marketDataService.getStockSnapshot(t))
          .catch(() => null),
      ]);

      if (snapshotResult?.success && snapshotResult.snapshot) {
        this.stockSnapshot.set(snapshotResult.snapshot);
      }

      this.availableExpirations.set(expirations);

      if (expirations.length > 0) {
        const today = new Date().toISOString().slice(0, 10);
        const nearest = expirations.find(e => e >= today) ?? expirations[0];
        this.selectedExpiration.set(nearest);
        await this.fetchChainSnapshot(t, nearest);
      }
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.expirationsLoading.set(false);
    }
  }

  async onExpirationSelected(date: string): Promise<void> {
    this.selectedExpiration.set(date);
    const t = this.ticker().trim().toUpperCase();
    if (t) {
      await this.fetchChainSnapshot(t, date);
    }
  }

  async fetchChainSnapshot(ticker: string, expiration: string): Promise<void> {
    this.chainLoading.set(true);
    this.error.set(null);

    try {
      const result = await firstValueFrom(
        this.marketDataService.getOptionsChainSnapshot(ticker, expiration)
      );

      if (!result.success) {
        this.error.set(result.error ?? 'Failed to fetch snapshot');
        return;
      }

      this.underlying.set(result.underlying);
      this.allContracts.set(result.contracts);

      setTimeout(() => this.scrollToAtm(), 100);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.chainLoading.set(false);
    }
  }

  async openContractOverlay(
    contract: SnapshotContractResult | null,
    side: 'call' | 'put'
  ): Promise<void> {
    if (!contract?.ticker) return;

    this.selectedContract.set({
      ticker: contract.ticker,
      contractType: side,
      strikePrice: contract.strikePrice!,
      expirationDate: contract.expirationDate!,
      snapshot: contract,
    });
    this.overlayVisible.set(true);
    this.overlayLoading.set(true);
    this.overlayError.set(null);
    this.overlayAggregates.set([]);

    try {
      const fromDate = getMinAllowedDate();
      const toDate = new Date().toISOString().slice(0, 10);
      const result = await firstValueFrom(
        this.marketDataService.getOrFetchStockAggregates(
          contract.ticker, fromDate, toDate, 'day', 1
        )
      );
      this.overlayAggregates.set(result.aggregates ?? []);
    } catch (err) {
      this.overlayError.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.overlayLoading.set(false);
    }
  }

  closeOverlay(): void {
    this.overlayVisible.set(false);
    this.selectedContract.set(null);
    this.overlayAggregates.set([]);
    this.overlayError.set(null);
  }

  toggleStrikeSort(): void {
    this.strikeSortDirection.update(d => d === 'asc' ? 'desc' : 'asc');
  }

  toggleShowAll(): void {
    this.showAllStrikes.update(v => !v);
  }

  // Formatting helpers (used for overlay display + pre-computed rows)
  formatIv(iv: number | null): string { return this.fmtIv(iv); }
  formatGreek(val: number | null): string { return this.fmtGreek(val); }
  formatNumber(val: number | null): string { return this.fmtNum(val); }
  formatPrice(val: number | null): string { return this.fmtPrice(val); }

  private fmtIv(iv: number | null): string {
    return iv != null ? (iv * 100).toFixed(1) + '%' : '\u2014';
  }

  private fmtGreek(val: number | null): string {
    return val != null ? val.toFixed(4) : '\u2014';
  }

  private fmtNum(val: number | null): string {
    return val != null ? val.toLocaleString() : '\u2014';
  }

  private fmtPrice(val: number | null): string {
    return val != null ? val.toFixed(2) : '\u2014';
  }

  /** Pick the best available price: day.close → lastTrade.price → lastQuote.midpoint → bid/ask mid */
  resolvePrice(c: SnapshotContractResult | null): string {
    if (!c) return '\u2014';
    if (c.day?.close != null) return c.day.close.toFixed(2);
    if (c.lastTrade?.price != null) return c.lastTrade.price.toFixed(2);
    if (c.lastQuote?.midpoint != null) return c.lastQuote.midpoint.toFixed(2);
    if (c.lastQuote?.bid != null && c.lastQuote?.ask != null) {
      return ((c.lastQuote.bid + c.lastQuote.ask) / 2).toFixed(2);
    }
    return '\u2014';
  }

  /** Format bid/ask spread as "B/A: X.XX / X.XX" */
  private fmtBidAsk(c: SnapshotContractResult | null): string {
    if (!c?.lastQuote) return '';
    const { bid, ask } = c.lastQuote;
    if (bid != null && ask != null) return `${bid.toFixed(2)} / ${ask.toFixed(2)}`;
    if (bid != null) return `${bid.toFixed(2)} / \u2014`;
    if (ask != null) return `\u2014 / ${ask.toFixed(2)}`;
    return '';
  }

  private barWidth(volume: number | null, max: number): number {
    if (!volume || !max) return 0;
    return (volume / max) * 100;
  }

  /** Parse option ticker like "O:SPY260223C00690000" into readable parts */
  private parseOptionTicker(ticker: string): ParsedOptionTicker | null {
    // Format: O:UNDERLYING YYMMDD C/P STRIKE(8 digits, 3 decimal places implied)
    const match = ticker.match(/^O:([A-Z]+)(\d{6})([CP])(\d{8})$/);
    if (!match) return null;

    const [, underlying, dateStr, typeChar, strikeStr] = match;

    // Parse date: YYMMDD
    const year = 2000 + parseInt(dateStr.slice(0, 2), 10);
    const month = parseInt(dateStr.slice(2, 4), 10) - 1;
    const day = parseInt(dateStr.slice(4, 6), 10);
    const date = new Date(year, month, day);

    const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

    // Parse strike: 8 digits with 3 implied decimals → divide by 1000
    const strike = parseInt(strikeStr, 10) / 1000;

    return {
      underlying,
      expDate: `${months[month]} ${day}, ${year}`,
      expDateShort: `${String(month + 1).padStart(2, '0')}/${String(day).padStart(2, '0')}/${String(year).slice(2)}`,
      type: typeChar === 'C' ? 'Call' : 'Put',
      strike: `$${strike.toFixed(2)}`,
    };
  }

  private scrollToAtm(): void {
    const el = document.querySelector('[data-atm="true"]');
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }
}
