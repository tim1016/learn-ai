import { HttpClient } from "@angular/common/http";
import { inject, Injectable } from "@angular/core";
import { firstValueFrom } from "rxjs";

import { environment } from "../../environments/environment";
import type { components } from "../api/broker.types";

export type LeanStrategySource = components["schemas"]["StrategyLeanSourceResponse"];

@Injectable({ providedIn: "root" })
export class LeanSourceService {
  private readonly http = inject(HttpClient);

  getStrategySource(strategyName: string): Promise<LeanStrategySource> {
    return firstValueFrom(
      this.http.get<LeanStrategySource>(
        `${environment.pythonServiceUrl}/api/engine/strategies/${encodeURIComponent(strategyName)}/lean-source`,
      ),
    );
  }
}
