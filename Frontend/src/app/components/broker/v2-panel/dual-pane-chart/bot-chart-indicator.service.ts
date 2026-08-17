import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import type { Observable } from 'rxjs';

import { environment } from '../../../../../environments/environment';
import type { ChartBar } from '../lib/broker-v2-panel.types';
import type { ChartIndicatorEntry } from '../../../../shared/trading-chart';
import {
  type ChartIndicatorBatchResponse,
  toChartIndicatorRequestBars,
} from './dual-pane-chart-indicators';

@Injectable({ providedIn: 'root' })
export class BotChartIndicatorService {
  private readonly http = inject(HttpClient);

  calculate(
    symbol: string,
    bars: readonly ChartBar[],
    indicators: readonly ChartIndicatorEntry[],
  ): Observable<ChartIndicatorBatchResponse> {
    return this.http.post<ChartIndicatorBatchResponse>(
      `${environment.pythonServiceUrl}/api/chart/indicators`,
      {
        symbol,
        bars: toChartIndicatorRequestBars(bars),
        indicators: indicators.map((indicator) => ({
          name: indicator.name,
          params: { ...indicator.params },
        })),
      },
    );
  }
}
