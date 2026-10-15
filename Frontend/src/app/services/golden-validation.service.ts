import { HttpClient, HttpParams } from "@angular/common/http";
import { inject, Injectable } from "@angular/core";
import { Observable } from "rxjs";

import { formatReceiptLabel } from "../shared/pipes/receipt-label.pipe";
import type {
  DesignateGoldenValidationRequest,
  GoldenValidation,
  ReviewGoldenValidationRequest,
} from "./golden-validation.types";

/**
 * How many of the newest cases one list read asks for. A reply exactly this
 * long may be cut short: older cases exist that it does not carry, so a
 * surface that shows the catalog says so.
 */
export const GOLDEN_VALIDATION_LIST_LIMIT = 100;

/**
 * A dossier state in words. The state names the latest review's decision even
 * after the evidence moved on, so a review that no longer applies says so
 * beside it: "Accepted" alone would read as a standing acceptance.
 */
export function goldenStateLabel(state: string, reviewIsCurrent: boolean | null | undefined): string {
  const label = formatReceiptLabel(state);
  return reviewIsCurrent === false ? `${label} · review no longer applies` : label;
}

/** Transport boundary for human-reviewed, immutable validation cases. */
@Injectable({ providedIn: "root" })
export class GoldenValidationService {
  private readonly http = inject(HttpClient);
  private readonly base = "/api/research/golden-validations";

  list(filters: { strategyName?: string; symbol?: string } = {}): Observable<GoldenValidation[]> {
    let params = new HttpParams().set("limit", GOLDEN_VALIDATION_LIST_LIMIT);
    if (filters.strategyName) params = params.set("strategy_name", filters.strategyName);
    if (filters.symbol) params = params.set("symbol", filters.symbol);
    return this.http.get<GoldenValidation[]>(this.base, { params });
  }

  designate(request: DesignateGoldenValidationRequest): Observable<GoldenValidation> {
    return this.http.post<GoldenValidation>(this.base, request);
  }

  review(id: number, request: ReviewGoldenValidationRequest): Observable<GoldenValidation> {
    return this.http.post<GoldenValidation>(`${this.base}/${id}/reviews`, request);
  }
}
