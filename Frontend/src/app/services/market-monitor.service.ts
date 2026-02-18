import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, map, tap, shareReplay } from 'rxjs';
import {
  MarketHolidaysResponse,
  MarketStatusResponse,
  MarketDashboardResponse,
  MarketHolidayEvent,
} from '../models/market-monitor';

const PYTHON_SERVICE_URL = 'http://localhost:8000';

@Injectable({ providedIn: 'root' })
export class MarketMonitorService {
  private http = inject(HttpClient);
  private holidaysCache$: Observable<MarketHolidayEvent[]> | null = null;

  getMarketStatus(): Observable<MarketStatusResponse> {
    return this.http
      .get<MarketStatusResponse>(`${PYTHON_SERVICE_URL}/api/market/status`)
      .pipe(
        tap(res => {
          if (!res.success) {
            throw new Error(res.error || 'Failed to fetch market status');
          }
        })
      );
  }

  getHolidays(limit: number = 20): Observable<MarketHolidayEvent[]> {
    if (!this.holidaysCache$) {
      this.holidaysCache$ = this.http
        .get<MarketHolidaysResponse>(
          `${PYTHON_SERVICE_URL}/api/market/holidays`,
          { params: { limit: limit.toString() } }
        )
        .pipe(
          tap(res => {
            if (!res.success) {
              throw new Error(res.error || 'Failed to fetch holidays');
            }
          }),
          map(res => res.events),
          shareReplay(1)
        );
    }
    return this.holidaysCache$;
  }

  getDashboard(): Observable<MarketDashboardResponse> {
    return this.http
      .get<MarketDashboardResponse>(`${PYTHON_SERVICE_URL}/api/market/dashboard`)
      .pipe(
        tap(res => {
          if (!res.success) {
            throw new Error(res.error || 'Failed to fetch dashboard');
          }
        })
      );
  }

  clearCache(): void {
    this.holidaysCache$ = null;
  }
}
