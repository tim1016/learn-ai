import { Component, inject, signal, computed, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { firstValueFrom } from 'rxjs';
import { MarketDataService } from '../../services/market-data.service';
import { SnapshotUnderlyingResult, SnapshotContractResult } from '../../graphql/types';
import { getMinAllowedDate } from '../../utils/date-validation';

@Component({
  selector: 'app-ticker-explorer',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './ticker-explorer.component.html',
  styleUrls: ['./ticker-explorer.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class TickerExplorerComponent {
  private marketDataService = inject(MarketDataService);

  minDate = getMinAllowedDate();

  ticker = signal('AAPL');
  expirationDate = signal(TickerExplorerComponent.getNextFriday()); // default to next Friday expiration
  loading = signal(false);
  error = signal<string | null>(null);

  underlying = signal<SnapshotUnderlyingResult | null>(null);
  allContracts = signal<SnapshotContractResult[]>([]);

  // Expiration date filter
  selectedExpiration = signal<string>('all');

  expirationDates = computed(() => {
    const dates = new Set<string>();
    for (const c of this.allContracts()) {
      if (c.expirationDate) dates.add(c.expirationDate);
    }
    return [...dates].sort();
  });

  filteredContracts = computed(() => {
    const contracts = this.allContracts();
    const exp = this.selectedExpiration();
    if (exp === 'all') return contracts;
    return contracts.filter(c => c.expirationDate === exp);
  });

  callContracts = computed(() =>
    this.filteredContracts()
      .filter(c => c.contractType === 'call')
      .sort((a, b) => (a.strikePrice ?? 0) - (b.strikePrice ?? 0))
  );

  putContracts = computed(() =>
    this.filteredContracts()
      .filter(c => c.contractType === 'put')
      .sort((a, b) => (a.strikePrice ?? 0) - (b.strikePrice ?? 0))
  );

  // Unique strikes for the chain table (sorted)
  strikes = computed(() => {
    const s = new Set<number>();
    for (const c of this.filteredContracts()) {
      if (c.strikePrice != null) s.add(c.strikePrice);
    }
    return [...s].sort((a, b) => a - b);
  });

  // Maps for quick lookup by strike
  callByStrike = computed(() => {
    const map = new Map<number, SnapshotContractResult>();
    for (const c of this.callContracts()) {
      if (c.strikePrice != null) map.set(c.strikePrice, c);
    }
    return map;
  });

  putByStrike = computed(() => {
    const map = new Map<number, SnapshotContractResult>();
    for (const c of this.putContracts()) {
      if (c.strikePrice != null) map.set(c.strikePrice, c);
    }
    return map;
  });

  // ATM strike = strike closest to underlying price
  atmStrike = computed(() => {
    const price = this.underlying()?.price ?? 0;
    const stks = this.strikes();
    if (stks.length === 0 || price === 0) return null;
    let closest = stks[0];
    let minDist = Math.abs(stks[0] - price);
    for (const s of stks) {
      const dist = Math.abs(s - price);
      if (dist < minDist) { closest = s; minDist = dist; }
    }
    return closest;
  });

  async fetchSnapshot(): Promise<void> {
    const t = this.ticker().trim().toUpperCase();
    if (!t) return;

    this.loading.set(true);
    this.error.set(null);
    this.underlying.set(null);
    this.allContracts.set([]);
    this.selectedExpiration.set('all');

    try {
      const exp = this.expirationDate() || undefined;
      const result = await firstValueFrom(
        this.marketDataService.getOptionsChainSnapshot(t, exp)
      );

      if (!result.success) {
        this.error.set(result.error ?? 'Failed to fetch snapshot');
        return;
      }

      this.underlying.set(result.underlying);
      this.allContracts.set(result.contracts);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : String(err));
    } finally {
      this.loading.set(false);
    }
  }

  isAtm(strike: number): boolean {
    return strike === this.atmStrike();
  }

  isItm(strike: number, type: 'call' | 'put'): boolean {
    const price = this.underlying()?.price ?? 0;
    if (price === 0) return false;
    return type === 'call' ? strike < price : strike > price;
  }

  formatIv(iv: number | null): string {
    return iv != null ? (iv * 100).toFixed(1) + '%' : '—';
  }

  formatGreek(val: number | null): string {
    return val != null ? val.toFixed(4) : '—';
  }

  formatNumber(val: number | null): string {
    return val != null ? val.toLocaleString() : '—';
  }

  formatPrice(val: number | null): string {
    return val != null ? val.toFixed(2) : '—';
  }

  /** Returns the next Friday (or today if it's Friday before market close). */
  private static getNextFriday(): string {
    const now = new Date();
    const day = now.getDay(); // 0=Sun, 5=Fri
    const daysUntilFriday = day <= 5 ? (5 - day) : (5 + 7 - day);
    const friday = new Date(now);
    friday.setDate(now.getDate() + (daysUntilFriday === 0 ? 0 : daysUntilFriday));
    return friday.toISOString().slice(0, 10);
  }
}
