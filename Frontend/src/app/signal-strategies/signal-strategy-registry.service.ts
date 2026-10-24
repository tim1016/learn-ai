import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { environment } from '../../environments/environment';
import type { StrategyInfo } from '../components/strategy-lab/strategy-lab.models';
import type { StrategyRegistryEntry } from './signal-strategy-evidence';

/**
 * The two reads that say what a registered strategy is: the registry's own
 * account of every entry and what it allows, and the Strategy Lab catalog's
 * declared parameter schemas. Proof, qualified versions and running bots keep
 * their own services; nothing here says a strategy is proven or approved.
 */
@Injectable({ providedIn: 'root' })
export class SignalStrategyRegistryService {
  private readonly http = inject(HttpClient);
  private readonly engine = `${environment.pythonServiceUrl}/api/engine`;

  /** Every registry entry, the ones Strategy Lab's picker hides included. */
  entries(): Promise<readonly StrategyRegistryEntry[]> {
    return firstValueFrom(this.http.get<StrategyRegistryEntry[]>(`${this.engine}/strategy-registry`));
  }

  /** The strategies Strategy Lab offers, each with its declared parameter schema. */
  parameterSchemas(): Promise<readonly StrategyInfo[]> {
    return firstValueFrom(this.http.get<StrategyInfo[]>(`${this.engine}/strategies`));
  }
}
