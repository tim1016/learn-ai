/**
 * Past-chain reconstruction service.
 *
 * Lifted from `options-history.component.ts:analyze()` during R1 of
 * the options-routes cleanup
 * (`docs/architecture/options-routes-research.md` § 5.1 R1).
 *
 * The Polygon Starter snapshot endpoint is live-only, so historical
 * chains can't be fetched as a single call. Instead, we construct OCC
 * tickers for ±5N strikes around an estimated ATM, batch-fetch their
 * daily aggregates 30 at a time, filter to the N closest strikes per
 * side that actually had data, and return a structured result the
 * inspector component can render directly.
 */

import { Injectable, inject } from '@angular/core';
import { catchError, firstValueFrom, forkJoin, of } from 'rxjs';
import { MarketDataService } from './market-data.service';
import { StockAggregate } from '../graphql/types';
import { formatTimestampDisplay } from '../shared/timestamp';
import { formatOcc } from '../utils/occ-ticker';

export interface PastChainContractRow {
  optionTicker: string;
  contractType: 'call' | 'put';
  strikePrice: number;
  dailyBar: StockAggregate | null;
  prevDayClose: number | null;
  changeFromPrevClose: number | null;
  changePercent: number | null;
  isAtm: boolean;
  /** Offset from ATM: 0 = ATM, +1 = first OTM call / first ITM put, etc. */
  relativeStrike: number;
}

export interface PastChainScanResult {
  strikePrice: number;
  callTicker: string;
  callHasData: boolean;
  putTicker: string;
  putHasData: boolean;
  /** Whether this strike made it into the final filtered chain. */
  selected: boolean;
}

export interface PastChainResult {
  /** Spot reference used to centre the chain. */
  atmPrice: number;
  /** Closest integer strike to atmPrice. */
  atmStrike: number;
  /** Previous trading day's close (used for change %). */
  prevDayClose: number | null;
  /** Open price on the analysis date (used as ATM when atmMethod = 'open'). */
  openPrice: number | null;
  /** All contract rows for the strikes that survived the data-availability filter. */
  contractRows: PastChainContractRow[];
  /** Audit table — every strike scanned + which ones had data + which ones made the cut. */
  scanResults: PastChainScanResult[];
  /** Stock minute bars on the analysis day, for the per-contract minute-detail modal. */
  stockMinuteBars: StockAggregate[];
}

export interface FetchPastChainInput {
  ticker: string;
  /** Analysis date (YYYY-MM-DD); the chain is reconstructed *as of* this date. */
  date: string;
  /** Number of strikes to surface on each side of ATM (default 5). */
  numStrikes: number;
  /** Whether to centre the chain on the day's open or on the previous close. */
  atmMethod: 'open' | 'prevClose';
}

@Injectable({ providedIn: 'root' })
export class PastChainService {
  private marketData = inject(MarketDataService);

  /** Match the legacy options-history component's batch size. */
  private static readonly BATCH_SIZE = 30;

  /** ±5× search range so we discover N strikes per side with actual data. */
  private static readonly SEARCH_MULTIPLIER = 5;

  /**
   * Reconstruct the chain on a past date.
   *
   * Throws if the underlying lacks both an open and a previous close on
   * the analysis date — without one of those we can't determine ATM.
   */
  async fetchPastChain(input: FetchPastChainInput): Promise<PastChainResult> {
    const ticker = input.ticker.trim().toUpperCase();
    const { date, atmMethod } = input;
    const numStrikes = Math.max(1, input.numStrikes);

    // Step 1 — stock daily bar on the analysis date (for open price).
    const dayResult = await firstValueFrom(
      this.marketData.getOrFetchStockAggregates(ticker, date, date, 'day', 1),
    );
    const dayBar = dayResult.aggregates?.[0];
    const openPrice = dayBar?.open ?? null;

    // Step 1b — stock minute bars on the analysis day (for minute-detail modal).
    let stockMinuteBars: StockAggregate[] = [];
    try {
      const minuteResult = await firstValueFrom(
        this.marketData.getOrFetchStockAggregates(ticker, date, date, 'minute', 1),
      );
      stockMinuteBars = minuteResult.aggregates ?? [];
    } catch {
      // Non-fatal — minute data may be missing for very old dates.
    }

    // Step 2 — previous trading day's close.
    const prevFrom = PastChainService.subtractDays(date, 7);
    const prevTo = PastChainService.subtractDays(date, 1);
    const prevResult = await firstValueFrom(
      this.marketData.getOrFetchStockAggregates(ticker, prevFrom, prevTo, 'day', 1),
    );
    const prevBars = prevResult.aggregates ?? [];
    const prevDayClose = prevBars.length > 0 ? prevBars[prevBars.length - 1].close : null;

    // Step 3 — pick the ATM reference price.
    const rawAtm = atmMethod === 'open' ? openPrice : prevDayClose;
    if (rawAtm == null) {
      throw new Error(
        `Could not determine ATM price. No ${atmMethod === 'open' ? 'opening' : 'previous close'} data found for ${ticker} on ${date}.`,
      );
    }
    const atmStrike = Math.round(rawAtm);

    // Step 4 — search a wider range so we find numStrikes per side with data.
    const searchRange = numStrikes * PastChainService.SEARCH_MULTIPLIER;
    const offsets: number[] = [];
    for (let i = -searchRange; i <= searchRange; i++) offsets.push(i);

    // Step 5 — construct OCC tickers for every candidate (call + put).
    interface TickerEntry {
      optionTicker: string;
      contractType: 'call' | 'put';
      strikePrice: number;
      offset: number;
    }
    const tickerEntries: TickerEntry[] = [];
    for (const offset of offsets) {
      const strike = atmStrike + offset;
      if (strike <= 0) continue;
      const callTicker = formatOcc({ underlying: ticker, expirationDate: date, contractType: 'call', strike });
      const putTicker = formatOcc({ underlying: ticker, expirationDate: date, contractType: 'put', strike });
      tickerEntries.push({ optionTicker: callTicker, contractType: 'call', strikePrice: strike, offset });
      tickerEntries.push({ optionTicker: putTicker, contractType: 'put', strikePrice: strike, offset });
    }

    // Step 6 — batch-fetch daily aggregates over a 2-day window (analysis day + prev close).
    const prevDayIso = prevBars.length > 0
      ? ymdEt(prevBars[prevBars.length - 1].timestamp)
      : prevTo;

    const ohlcResults: { aggregates?: StockAggregate[] | null }[] = [];
    for (let bi = 0; bi < tickerEntries.length; bi += PastChainService.BATCH_SIZE) {
      const batch = tickerEntries.slice(bi, bi + PastChainService.BATCH_SIZE);
      const observables = batch.map(entry =>
        this.marketData
          .getOrFetchStockAggregates(entry.optionTicker, prevDayIso, date, 'day', 1)
          .pipe(catchError(() => of({ ticker: entry.optionTicker, aggregates: [] as StockAggregate[], summary: null }))),
      );
      const batchResults = await firstValueFrom(forkJoin(observables));
      ohlcResults.push(...batchResults);
    }

    // Step 7 — build all candidate contract rows.
    const allRows: PastChainContractRow[] = tickerEntries.map((entry, i) => {
      const result = ohlcResults[i];
      const bars = result.aggregates ?? [];

      let analysisDayBar: StockAggregate | null = null;
      let prevDayBar: StockAggregate | null = null;
      for (const bar of bars) {
        const barDate = ymdEt(bar.timestamp);
        if (barDate === date) analysisDayBar = bar;
        else if (barDate < date) prevDayBar = bar;
      }
      // Fallback: if no exact-date match, treat the latest bar as analysis day.
      if (!analysisDayBar && bars.length > 0) {
        analysisDayBar = bars[bars.length - 1];
        if (bars.length > 1) prevDayBar = bars[bars.length - 2];
      }

      const pdc = prevDayBar?.close ?? null;
      const dayClose = analysisDayBar?.close ?? null;
      const change = dayClose != null && pdc != null ? dayClose - pdc : null;
      const changePct = change != null && pdc != null && pdc !== 0 ? (change / pdc) * 100 : null;

      return {
        optionTicker: entry.optionTicker,
        contractType: entry.contractType,
        strikePrice: entry.strikePrice,
        dailyBar: analysisDayBar,
        prevDayClose: pdc,
        changeFromPrevClose: change,
        changePercent: changePct,
        isAtm: entry.offset === 0,
        relativeStrike: entry.offset,
      };
    });

    // Step 8 — filter to the N closest strikes per side that had data.
    const strikeHasData = new Set<number>();
    for (const row of allRows) {
      if (row.dailyBar != null) strikeHasData.add(row.strikePrice);
    }
    const strikesWithData = [...strikeHasData].sort((a, b) => a - b);
    const above = strikesWithData.filter(s => s > atmStrike).slice(0, numStrikes);
    const below = strikesWithData.filter(s => s < atmStrike).slice(-numStrikes);
    const atmIfPresent = strikeHasData.has(atmStrike) ? [atmStrike] : [];
    const selectedStrikes = new Set([...below, ...atmIfPresent, ...above]);

    // Build scan-results audit table — every strike with at least one side that had data.
    const scanMap = new Map<number, { call?: PastChainContractRow; put?: PastChainContractRow }>();
    for (const row of allRows) {
      if (!scanMap.has(row.strikePrice)) scanMap.set(row.strikePrice, {});
      const entry = scanMap.get(row.strikePrice)!;
      if (row.contractType === 'call') entry.call = row;
      else entry.put = row;
    }
    const scanResults: PastChainScanResult[] = [...scanMap.entries()]
      .sort(([a], [b]) => a - b)
      .filter(([, v]) => v.call?.dailyBar != null || v.put?.dailyBar != null)
      .map(([strike, v]) => ({
        strikePrice: strike,
        callTicker: v.call?.optionTicker ?? '',
        callHasData: v.call?.dailyBar != null,
        putTicker: v.put?.optionTicker ?? '',
        putHasData: v.put?.dailyBar != null,
        selected: selectedStrikes.has(strike),
      }));

    const filteredRows = allRows.filter(row => selectedStrikes.has(row.strikePrice));

    return {
      atmPrice: rawAtm,
      atmStrike,
      prevDayClose,
      openPrice,
      contractRows: filteredRows,
      scanResults,
      stockMinuteBars,
    };
  }

  /** Subtract `days` calendar days from an ISO YYYY-MM-DD string. */
  private static subtractDays(dateStr: string, days: number): string {
    const d = new Date(dateStr + 'T00:00:00');
    d.setDate(d.getDate() - days);
    return d.toISOString().slice(0, 10);
  }
}

function ymdEt(ms: number): string {
  return formatTimestampDisplay(ms, { mode: 'date-et' });
}
