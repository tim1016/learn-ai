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
