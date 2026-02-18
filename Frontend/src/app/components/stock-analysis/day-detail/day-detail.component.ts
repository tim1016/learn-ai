import { Component, inject, signal, computed, ChangeDetectionStrategy, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { MarketDataService } from '../../../services/market-data.service';
import { StockAggregate, OptionsContract } from '../../../graphql/types';
import { LineChartComponent } from '../../market-data/line-chart/line-chart.component';
import { VolumeChartComponent } from '../../market-data/volume-chart/volume-chart.component';
import { AtmMethod, SelectedContract } from '../models';
import { selectNearAtmContracts } from '../utils';

interface ContractChartData {
  contract: SelectedContract;
  bars: StockAggregate[];
}

@Component({
  selector: 'app-day-detail',
  standalone: true,
  imports: [CommonModule, RouterLink, LineChartComponent, VolumeChartComponent],
  templateUrl: './day-detail.component.html',
  styleUrls: ['./day-detail.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class DayDetailComponent implements OnInit {
  private route = inject(ActivatedRoute);
  private marketDataService = inject(MarketDataService);

  ticker = signal('');
  date = signal('');
  atmMethod = signal<AtmMethod>('previousClose');

  stockBars = signal<StockAggregate[]>([]);
  callContracts = signal<ContractChartData[]>([]);
  putContracts = signal<ContractChartData[]>([]);

  loading = signal(true);
  error = signal<string | null>(null);
  optionsLoading = signal(false);
  optionsProgress = signal({ loaded: 0, total: 0 });

  atmPrice = computed(() => {
    const bars = this.stockBars();
    if (bars.length === 0) return 0;
    const sorted = [...bars].sort(
      (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
    );
    if (this.atmMethod() === 'currentOpen') {
      return sorted[0].open;
    }
    return sorted[sorted.length - 1].close;
  });

  ngOnInit(): void {
    const params = this.route.snapshot.params;
    const queryParams = this.route.snapshot.queryParams;

    this.ticker.set(params['ticker'] ?? '');
    this.date.set(params['date'] ?? '');
    if (queryParams['atm']) {
      this.atmMethod.set(queryParams['atm'] as AtmMethod);
    }

    this.loadData();
  }

  private async loadData(): Promise<void> {
    const t = this.ticker();
    const d = this.date();
    if (!t || !d) {
      this.error.set('Missing ticker or date');
      this.loading.set(false);
      return;
    }

    try {
      // Load stock data for this day (should be cached from analysis)
      const result = await firstValueFrom(
        this.marketDataService.getOrFetchStockAggregates(t, d, d, 'minute', 1)
      );
      this.stockBars.set(result.aggregates);
      this.loading.set(false);

      // Now load options
      await this.loadOptions();
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
      this.loading.set(false);
    }
  }

  private async loadOptions(): Promise<void> {
    const t = this.ticker();
    const d = this.date();
    const bars = this.stockBars();
    if (bars.length === 0) return;

    this.optionsLoading.set(true);

    try {
      // Calculate ATM price
      const sorted = [...bars].sort(
        (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
      );
      const price = this.atmMethod() === 'currentOpen'
        ? sorted[0].open
        : sorted[sorted.length - 1].close;

      if (price <= 0) {
        this.optionsLoading.set(false);
        return;
      }

      // Query 0DTE contracts
      const buffer = Math.max(price * 0.05, 5);
      const contractsResult = await firstValueFrom(
        this.marketDataService.getOptionsContracts(t, {
          asOfDate: d,
          strikePriceGte: Math.floor(price - buffer),
          strikePriceLte: Math.ceil(price + buffer),
          expirationDate: d,
          limit: 200,
        })
      );

      if (!contractsResult.success || contractsResult.contracts.length === 0) {
        this.optionsLoading.set(false);
        return;
      }

      // Select ATM + 2 ITM + 2 OTM
      const selected = selectNearAtmContracts(contractsResult.contracts, price, 2, 2);
      const calls = selected
        .filter(c => c.contractType === 'call')
        .sort((a, b) => (a.strikePrice ?? 0) - (b.strikePrice ?? 0));
      const puts = selected
        .filter(c => c.contractType === 'put')
        .sort((a, b) => (a.strikePrice ?? 0) - (b.strikePrice ?? 0));

      this.optionsProgress.set({ loaded: 0, total: selected.length });

      // Fetch bars for each contract (should be cached)
      const callData: ContractChartData[] = [];
      const putData: ContractChartData[] = [];
      let loaded = 0;

      for (const contract of calls) {
        try {
          const res = await firstValueFrom(
            this.marketDataService.getOrFetchStockAggregates(contract.ticker, d, d, 'minute', 1)
          );
          callData.push({
            contract: {
              ticker: contract.ticker,
              contractType: contract.contractType ?? 'call',
              strikePrice: contract.strikePrice ?? 0,
              expirationDate: contract.expirationDate ?? d,
            },
            bars: res.aggregates,
          });
        } catch {
          // Skip failed contract
        }
        loaded++;
        this.optionsProgress.set({ loaded, total: selected.length });
      }

      for (const contract of puts) {
        try {
          const res = await firstValueFrom(
            this.marketDataService.getOrFetchStockAggregates(contract.ticker, d, d, 'minute', 1)
          );
          putData.push({
            contract: {
              ticker: contract.ticker,
              contractType: contract.contractType ?? 'put',
              strikePrice: contract.strikePrice ?? 0,
              expirationDate: contract.expirationDate ?? d,
            },
            bars: res.aggregates,
          });
        } catch {
          // Skip failed contract
        }
        loaded++;
        this.optionsProgress.set({ loaded, total: selected.length });
      }

      this.callContracts.set(callData);
      this.putContracts.set(putData);
    } catch (err) {
      console.error('Failed to load options:', err);
    }

    this.optionsLoading.set(false);
  }

  formatStrike(price: number): string {
    return '$' + price.toFixed(2);
  }
}
