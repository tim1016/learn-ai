import { provideZonelessChangeDetection } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { provideHttpClient } from "@angular/common/http";
import {
  HttpTestingController,
  provideHttpClientTesting,
} from "@angular/common/http/testing";
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";

import { LeanScriptEditorComponent } from "./lean-script-editor.component";
import {
  EMA_CROSSOVER_SOURCE_TEMPLATE,
} from "./lean-script-editor.template";
import { environment } from "../../../environments/environment";

describe("LeanScriptEditorComponent", () => {
  let httpMock: HttpTestingController;

  beforeEach(() => {
    vi.useFakeTimers();
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        provideHttpClient(),
        provideHttpClientTesting(),
      ],
    });
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("seeds the EMA-crossover template on first mount", () => {
    const fixture = TestBed.createComponent(LeanScriptEditorComponent);
    fixture.detectChanges();
    const component = fixture.componentInstance;
    expect(component.source()).toBe(EMA_CROSSOVER_SOURCE_TEMPLATE);
    expect(component.source()).toContain("class MyAlgorithm");
  });

  it("seed reads symbol/window/cash from GetParameter (no hardcoded values)", () => {
    // Regression: hardcoded SetStartDate / AddEquity("SPY", ...) caused
    // silent runtime errors when the form selected a different symbol
    // or window. The seed must teach the parameterised pattern so users
    // copying from it don't fall into that trap. Fixtures are the only
    // place where hardcoding is appropriate (they pin specific bars).
    expect(EMA_CROSSOVER_SOURCE_TEMPLATE).toContain('self.GetParameter("symbol")');
    expect(EMA_CROSSOVER_SOURCE_TEMPLATE).toContain('self.GetParameter("start_date")');
    expect(EMA_CROSSOVER_SOURCE_TEMPLATE).toContain('self.GetParameter("end_date")');
    expect(EMA_CROSSOVER_SOURCE_TEMPLATE).toContain('self.GetParameter("starting_cash")');

    // Negative pin: no literal hardcoded ticker or year inside Initialize.
    // ``self.AddEquity(symbol_str, ...)`` is the only AddEquity call;
    // a quoted ticker before that line would indicate regression.
    const initializeBody = EMA_CROSSOVER_SOURCE_TEMPLATE.split("def Initialize(self):")[1].split("def OnData")[0];
    expect(initializeBody).not.toMatch(/AddEquity\(\s*"[A-Z]+"/);
    // No literal year tuple to SetStartDate/SetEndDate.
    expect(initializeBody).not.toMatch(/SetStartDate\(\s*\d{4}\s*,/);
    expect(initializeBody).not.toMatch(/SetEndDate\(\s*\d{4}\s*,/);
  });

  it("seed mirrors SpyEmaCrossover strategy semantics (parity-critical markers)", () => {
    // Regression: the editor default must produce the same trades as the
    // python ``SpyEmaCrossover`` strategy when both run on the same
    // window. Pinning the parity-critical markers here so the template
    // can't silently regress to a simpler EMA-only crossover (no RSI,
    // no gap threshold, exit-on-recross instead of time-stop).
    // Canonical oracle: PythonDataService/app/lean_sidecar/trusted_samples/ema_crossover.py
    expect(EMA_CROSSOVER_SOURCE_TEMPLATE).toContain("TradeBarConsolidator(timedelta(minutes=15))");
    expect(EMA_CROSSOVER_SOURCE_TEMPLATE).toContain("RelativeStrengthIndex");
    expect(EMA_CROSSOVER_SOURCE_TEMPLATE).toContain("MovingAverageType.Wilders");
    expect(EMA_CROSSOVER_SOURCE_TEMPLATE).toContain("RSI_PERIOD = 14");
    expect(EMA_CROSSOVER_SOURCE_TEMPLATE).toContain("EXIT_BARS = 5");
    expect(EMA_CROSSOVER_SOURCE_TEMPLATE).toContain("GAP_MIN = 0.20");
    expect(EMA_CROSSOVER_SOURCE_TEMPLATE).toContain("self.prev_fast <= self.prev_slow");
  });

  it("seed pins a constant benchmark so copied QQQ runs avoid unstaged SPY data", () => {
    // Regression: LEAN's default benchmark subscribes to SPY hour/daily
    // files even when the user-selected symbol is QQQ. The sidecar
    // stages the selected symbol only, so the starter template must
    // carry the same constant benchmark pin as the trusted samples.
    const initializeBody = EMA_CROSSOVER_SOURCE_TEMPLATE.split("def Initialize(self):")[1]
      .split("def OnData")[0];
    expect(initializeBody).toContain("self.SetBenchmark(lambda dt: 100)");
  });

  it("propagates source updates through the model signal", () => {
    const fixture = TestBed.createComponent(LeanScriptEditorComponent);
    fixture.detectChanges();
    const component = fixture.componentInstance;

    component.source.set("# new code\n");
    expect(component.source()).toBe("# new code\n");
  });

  it("debounces lint requests to 500ms", () => {
    const fixture = TestBed.createComponent(LeanScriptEditorComponent);
    fixture.detectChanges();
    const component = fixture.componentInstance;

    // Initial mount: drain the seed-template request (fires after the debounce).
    vi.advanceTimersByTime(500);
    httpMock
      .expectOne(`${environment.pythonServiceUrl}/api/lean-sidecar/lint`)
      .flush({ diagnostics: [] });

    component.source.set("x = 1\n");
    vi.advanceTimersByTime(250);
    httpMock.expectNone(`${environment.pythonServiceUrl}/api/lean-sidecar/lint`);

    vi.advanceTimersByTime(300);
    const req = httpMock.expectOne(
      `${environment.pythonServiceUrl}/api/lean-sidecar/lint`,
    );
    expect(req.request.body).toEqual({ source: "x = 1\n" });
    req.flush({ diagnostics: [] });
  });

  it("renders ruff diagnostics in the Problems panel", () => {
    const fixture = TestBed.createComponent(LeanScriptEditorComponent);
    fixture.detectChanges();
    const component = fixture.componentInstance;

    vi.advanceTimersByTime(500);
    httpMock
      .expectOne(`${environment.pythonServiceUrl}/api/lean-sidecar/lint`)
      .flush({
        diagnostics: [
          {
            line: 3,
            col: 1,
            end_line: 3,
            end_col: 14,
            rule: "F401",
            severity: "warning",
            message: "'pandas' imported but unused",
            fix: null,
          },
        ],
      });

    fixture.detectChanges();
    expect(component.diagnostics()).toHaveLength(1);
    expect(component.diagnostics()[0].rule).toBe("F401");

    const html = fixture.nativeElement.textContent as string;
    expect(html).toContain("F401");
    expect(html).toContain("pandas");
  });

  it("scrolls the editor to a clicked diagnostic's line", () => {
    const fixture = TestBed.createComponent(LeanScriptEditorComponent);
    fixture.detectChanges();
    const component = fixture.componentInstance;

    vi.advanceTimersByTime(500);
    httpMock
      .expectOne(`${environment.pythonServiceUrl}/api/lean-sidecar/lint`)
      .flush({ diagnostics: [] });

    const scrollSpy = vi.fn();
    component.scrollEditorToLine = scrollSpy;
    component.onDiagnosticClick({
      line: 12,
      col: 1,
      end_line: null,
      end_col: null,
      rule: "E501",
      severity: "warning",
      message: "line too long",
      fix: null,
    });
    expect(scrollSpy).toHaveBeenCalledWith(12);
  });
});
