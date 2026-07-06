import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type {
  StrategyValidationCatalog,
  StrategyValidationDetail,
  StrategyValidationFlagRequest,
  StrategyValidationRefreshResult,
} from './strategy-validation.types';

@Injectable({ providedIn: 'root' })
export class StrategyValidationService {
  private readonly http = inject(HttpClient);
  private readonly base = '/api/strategy-validation/strategies';

  getCatalog(): Promise<StrategyValidationCatalog> {
    return firstValueFrom(this.http.get<StrategyValidationCatalog>(this.base));
  }

  getDetail(strategyKey: string): Promise<StrategyValidationDetail> {
    return firstValueFrom(
      this.http.get<StrategyValidationDetail>(`${this.base}/${encodeURIComponent(strategyKey)}`),
    );
  }

  refreshValidationEvidence(strategyKey: string): Promise<StrategyValidationRefreshResult> {
    return firstValueFrom(
      this.http.post<StrategyValidationRefreshResult>(
        `${this.base}/${encodeURIComponent(strategyKey)}/refresh`,
        {},
      ),
    );
  }

  flagValidation(
    strategyKey: string,
    request: StrategyValidationFlagRequest,
  ): Promise<StrategyValidationDetail> {
    return firstValueFrom(
      this.http.post<StrategyValidationDetail>(
        `${this.base}/${encodeURIComponent(strategyKey)}/flag`,
        request,
      ),
    );
  }
}
