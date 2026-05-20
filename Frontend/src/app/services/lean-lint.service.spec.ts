import { provideZonelessChangeDetection } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { provideHttpClient } from "@angular/common/http";
import {
  HttpTestingController,
  provideHttpClientTesting,
} from "@angular/common/http/testing";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { firstValueFrom } from "rxjs";

import { LeanLintService, type Diagnostic } from "./lean-lint.service";
import { environment } from "../../environments/environment";

describe("LeanLintService", () => {
  let service: LeanLintService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        LeanLintService,
        provideHttpClient(),
        provideHttpClientTesting(),
      ],
    });
    service = TestBed.inject(LeanLintService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  it("POSTs {source} to /api/lean-sidecar/lint and returns the diagnostics", async () => {
    const diagnostic: Diagnostic = {
      line: 12,
      col: 5,
      end_line: 12,
      end_col: 18,
      rule: "F401",
      severity: "warning",
      message: "'pandas' imported but unused",
      fix: null,
    };

    const promise = firstValueFrom(service.lint("import pandas\n"));

    const req = httpMock.expectOne(
      `${environment.pythonServiceUrl}/api/lean-sidecar/lint`,
    );
    expect(req.request.method).toBe("POST");
    expect(req.request.body).toEqual({ source: "import pandas\n" });
    req.flush({ diagnostics: [diagnostic] });

    const result = await promise;
    expect(result.diagnostics).toHaveLength(1);
    expect(result.diagnostics[0].rule).toBe("F401");
  });

  it("returns an empty list when ruff finds no issues", async () => {
    const promise = firstValueFrom(service.lint("x = 1\n"));

    const req = httpMock.expectOne(
      `${environment.pythonServiceUrl}/api/lean-sidecar/lint`,
    );
    req.flush({ diagnostics: [] });

    const result = await promise;
    expect(result.diagnostics).toEqual([]);
  });
});
