import { provideHttpClient } from "@angular/common/http";
import { HttpTestingController, provideHttpClientTesting } from "@angular/common/http/testing";
import { TestBed } from "@angular/core/testing";
import { firstValueFrom } from "rxjs";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { environment } from "../../environments/environment";
import { GoldenValidationService } from "./golden-validation.service";

const BASE = `${environment.pythonServiceUrl}/api/research/golden-validations`;

describe("GoldenValidationService", () => {
  let service: GoldenValidationService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    service = TestBed.inject(GoldenValidationService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it("lists the Golden Validation catalog with optional scope filters", async () => {
    const pending = firstValueFrom(service.list({ strategyName: "ema_crossover", symbol: "SPY" }));
    const request = http.expectOne((entry) => entry.url === BASE);

    expect(request.request.method).toBe("GET");
    expect(request.request.params.get("strategy_name")).toBe("ema_crossover");
    expect(request.request.params.get("symbol")).toBe("SPY");
    request.flush([]);

    expect(await pending).toEqual([]);
  });

  it("submits designation and review commands to their immutable endpoints", async () => {
    const designation = firstValueFrom(service.designate({
      source_run_id: 44,
      command_id: "designate-1",
      rationale: "Selected as the representative SPY baseline.",
    }));
    const designateRequest = http.expectOne(BASE);
    expect(designateRequest.request.method).toBe("POST");
    expect(designateRequest.request.body.source_run_id).toBe(44);
    designateRequest.flush({ id: 8 });
    await designation;

    const review = firstValueFrom(service.review(8, {
      command_id: "review-1",
      expected_evidence_revision: "a".repeat(64),
      decision: "accept",
      reason: "Reviewed the one extra LEAN trade.",
    }));
    const reviewRequest = http.expectOne(`${BASE}/8/reviews`);
    expect(reviewRequest.request.method).toBe("POST");
    expect(reviewRequest.request.body.decision).toBe("accept");
    reviewRequest.flush({ id: 8 });
    await review;
  });
});
