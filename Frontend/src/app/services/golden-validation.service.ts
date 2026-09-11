import { HttpClient, HttpParams } from "@angular/common/http";
import { inject, Injectable } from "@angular/core";
import { Observable } from "rxjs";

import type {
  DesignateGoldenValidationRequest,
  GoldenValidation,
  ReviewGoldenValidationRequest,
} from "./golden-validation.types";

/** Transport boundary for human-reviewed, immutable validation cases. */
@Injectable({ providedIn: "root" })
export class GoldenValidationService {
  private readonly http = inject(HttpClient);
  private readonly base = "/api/research/golden-validations";

  list(filters: { strategyName?: string; symbol?: string } = {}): Observable<GoldenValidation[]> {
    let params = new HttpParams();
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
