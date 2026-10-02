import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type {
  CustomGate,
  CustomGateSave,
  GateCatalogue,
  GateEvaluationRequest,
  GateEvaluationResponse,
  StrategyGateList,
} from '../lib/broker-v2-panel.types';

const STRATEGY_GATES_BASE = '/api/strategy-gates';

/**
 * Custom Dark Bright Gates on the data plane (#2639): saved per strategy, so
 * every bot running it and Strategy Lab read the same list, and judged there
 * on the candles the page already shows. The base is relative so the dev
 * proxy attaches the data-plane control secret to writes; the shared
 * interceptor adds the control intent it checks for.
 */
@Injectable({ providedIn: 'root' })
export class StrategyGatesService {
  private readonly http = inject(HttpClient);

  private url(strategyKey: string, ...rest: string[]): string {
    return [STRATEGY_GATES_BASE, strategyKey, ...rest]
      .map((part, index) => (index === 0 ? part : encodeURIComponent(part)))
      .join('/');
  }

  /** The catalogue indicators a gate can read, from the same rules that judge one. */
  catalogue(): Promise<GateCatalogue> {
    return firstValueFrom(this.http.get<GateCatalogue>(`${STRATEGY_GATES_BASE}/catalogue`));
  }

  list(strategyKey: string): Promise<StrategyGateList> {
    return firstValueFrom(this.http.get<StrategyGateList>(this.url(strategyKey)));
  }

  create(strategyKey: string, gate: CustomGateSave): Promise<CustomGate> {
    return firstValueFrom(this.http.post<CustomGate>(this.url(strategyKey), gate));
  }

  replace(strategyKey: string, gateId: string, gate: CustomGateSave): Promise<CustomGate> {
    return firstValueFrom(this.http.put<CustomGate>(this.url(strategyKey, gateId), gate));
  }

  async remove(strategyKey: string, gateId: string): Promise<void> {
    await firstValueFrom(this.http.delete(this.url(strategyKey, gateId)));
  }

  evaluate(strategyKey: string, request: GateEvaluationRequest): Promise<GateEvaluationResponse> {
    return firstValueFrom(this.http.post<GateEvaluationResponse>(this.url(strategyKey, 'evaluate'), request));
  }
}
