import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type {
  StrategyValidationCatalog,
  StrategyValidationDetail,
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
}
