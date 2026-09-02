import { HttpClient, HttpErrorResponse } from "@angular/common/http";
import { inject, Injectable } from "@angular/core";
import { catchError, firstValueFrom, map, of } from "rxjs";

import { environment } from "../../environments/environment";
import type { components } from "../api/broker.types";

export type LeanStrategySource = components["schemas"]["StrategyLeanSourceResponse"];

/**
 * A strategy with no `lean_twin` is a fact about the strategy — 4 of 7
 * registered strategies are in that state — not a system failure. Keeping the
 * two apart stops the UI reporting "unavailable" for something that was never
 * registered in the first place.
 */
export type LeanSourceResult =
  | { readonly kind: "available"; readonly source: LeanStrategySource }
  | { readonly kind: "unregistered"; readonly detail: string }
  | { readonly kind: "unavailable"; readonly detail: string };

@Injectable({ providedIn: "root" })
export class LeanSourceService {
  private readonly http = inject(HttpClient);

  getStrategySource(strategyName: string): Promise<LeanSourceResult> {
    return firstValueFrom(
      this.http
        .get<LeanStrategySource>(
          `${environment.pythonServiceUrl}/api/engine/strategies/${encodeURIComponent(strategyName)}/lean-source`,
        )
        .pipe(
          map((source): LeanSourceResult => ({ kind: "available", source })),
          catchError((error: unknown) => of(toFailure(error))),
        ),
    );
  }
}

function toFailure(error: unknown): LeanSourceResult {
  if (error instanceof HttpErrorResponse && error.status === 404) {
    return { kind: "unregistered", detail: detailOf(error) ?? "This strategy has no registered LEAN validation source." };
  }
  return {
    kind: "unavailable",
    detail: "The registered QCAlgorithm source could not be loaded.",
  };
}

function detailOf(error: HttpErrorResponse): string | null {
  const body: unknown = error.error;
  if (typeof body !== "object" || body === null) return null;
  const detail = (body as Record<string, unknown>)["detail"];
  return typeof detail === "string" && detail.trim() ? detail : null;
}
