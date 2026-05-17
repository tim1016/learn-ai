import { provideZonelessChangeDetection } from "@angular/core";
import { ComponentFixture, TestBed } from "@angular/core/testing";
import { describe, expect, it, vi } from "vitest";
import { LeanSidecarApiError, LeanSidecarService } from "../../services/lean-sidecar.service";
import type { NormalizedResult, TrustedRunResponse } from "../../services/lean-sidecar.types";
import { LeanLabComponent } from "./lean-lab.component";

/**
 * Component-level tests. Asserts what the operator sees on the page,
 * not the internal signal values. The service is faked at the DI
 * level so these tests run without an HTTP boundary or a real
 * launcher.
 */

interface FakeLeanSidecarService {
  startTrustedRun: ReturnType<typeof vi.fn>;
  getNormalized: ReturnType<typeof vi.fn>;
  getLogTail: ReturnType<typeof vi.fn>;
  getObservationsCsv: ReturnType<typeof vi.fn>;
}

function makeResponse(overrides: Partial<TrustedRunResponse> = {}): TrustedRunResponse {
  return {
    run_id: "ui_run_20260517000000",
    is_clean: true,
    exit_code: 0,
    duration_ms: 1234,
    timed_out: false,
    lean_errors: {
      analysis_failed: [],
      failed_data_requests: [],
      runtime_error: [],
      other: [],
    },
    log_tail: "LEAN ALGORITHMIC TRADING ENGINE v2.5.0.0\n",
    manifest_path: "/tmp/ws/manifest.json",
    workspace_root: "/tmp/ws",
    observations_path: "/tmp/ws/workspace/output/storage/observations.csv",
    lean_log_path: "/tmp/ws/workspace/output/log.txt",
    normalized_path: "/tmp/ws/normalized/result.json",
    normalized_parser_version: "phase-3a-r1",
    total_order_events: 2,
    total_equity_points: 30,
    ...overrides,
  };
}

function makeNormalized(): NormalizedResult {
  return {
    parser_version: "phase-3a-r1",
    algorithm_id: "MyAlgorithm",
    statistics: { "Total Orders": "1", "Sharpe Ratio": "0" },
    runtime_statistics: {},
    equity_curve: [
      { ms_utc: 1_736_121_600_000, value: 100_000, open: 100_000, high: 100_000, low: 100_000 },
      { ms_utc: 1_736_467_200_000, value: 100_284.14, open: 100_284.14, high: 100_284.14, low: 100_284.14 },
    ],
    order_events: [],
    total_order_events: 2,
    total_equity_points: 30,
    first_equity_ms_utc: 1_736_121_600_000,
    last_equity_ms_utc: 1_736_467_200_000,
  };
}

describe("LeanLabComponent", () => {
  let fixture: ComponentFixture<LeanLabComponent>;
  let component: LeanLabComponent;
  let serviceMock: FakeLeanSidecarService;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    serviceMock = {
      startTrustedRun: vi.fn(),
      getNormalized: vi.fn(),
      getLogTail: vi.fn(),
      getObservationsCsv: vi.fn(),
    };
    await TestBed.configureTestingModule({
      imports: [LeanLabComponent],
      providers: [
        provideZonelessChangeDetection(),
        { provide: LeanSidecarService, useValue: serviceMock },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(LeanLabComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it("renders the form heading and defaults", () => {
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector("h1")?.textContent).toContain("LEAN Sidecar Lab");
    expect(component.form.controls.symbol.value).toBe("SPY");
    expect(component.form.controls.startingCash.value).toBe(100_000);
  });

  it("shows the clean-run badge + summary after a successful submit", async () => {
    serviceMock.startTrustedRun.mockResolvedValue(makeResponse());
    serviceMock.getNormalized.mockResolvedValue(makeNormalized());

    await component.submit();
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("Clean run");
    expect(text).toContain("phase-3a-r1");
    expect(text).toContain("Equity snapshot");
    // P&L line shows when normalized result is loaded.
    expect(text).toContain("100,000.00");
  });

  it("renders the yellow LEAN-errors badge when is_clean is false but exit==0", async () => {
    serviceMock.startTrustedRun.mockResolvedValue(
      makeResponse({
        is_clean: false,
        lean_errors: {
          analysis_failed: [],
          failed_data_requests: ["File not found: /lean-run/data/equity/usa/minute/spy/20250106_quote.zip"],
          runtime_error: [],
          other: [],
        },
      }),
    );

    await component.submit();
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("LEAN errors logged");
    expect(text).toContain("failed_data_requests");
    expect(text).toContain("quote.zip");
  });

  it("renders the launcher's typed rejection envelope on a 400", async () => {
    serviceMock.startTrustedRun.mockRejectedValue(
      new LeanSidecarApiError(400, "workspace_not_staged", "stage first"),
    );

    await component.submit();
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("Run failed");
    expect(text).toContain("workspace_not_staged");
    expect(text).toContain("stage first");
  });
});
