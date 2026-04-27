import { ComponentFixture, TestBed } from "@angular/core/testing";
import { provideHttpClient } from "@angular/common/http";
import { provideHttpClientTesting } from "@angular/common/http/testing";
import { provideRouter } from "@angular/router";
import { RealizedVsIvComponent } from "./realized-vs-iv.component";
import {
  EdgeMockDataService,
  type EdgeData,
  type IvConfidenceSummary,
} from "../services/edge-mock-data.service";

/**
 * Banner-rendering tests for the IV-source/confidence panel added in
 * the Step E + recorder-fallback follow-up. Asserts on the rendered
 * DOM (data-testid hooks), not on private signal values, per
 * .claude/rules/angular.md "Testing".
 */
describe("RealizedVsIvComponent — IV confidence banner", () => {
  let fixture: ComponentFixture<RealizedVsIvComponent>;
  let component: RealizedVsIvComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [RealizedVsIvComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(RealizedVsIvComponent);
    component = fixture.componentInstance;
  });

  function setIvConfidence(iv: IvConfidenceSummary | null): void {
    const base = TestBed.inject(EdgeMockDataService).get();
    const next: EdgeData = { ...base, ivConfidence: iv };
    component.data.set(next);
    fixture.detectChanges();
  }

  function banner(): HTMLElement | null {
    return fixture.nativeElement.querySelector('[data-testid="iv-confidence-banner"]');
  }

  function bannerOrThrow(): HTMLElement {
    const el = banner();
    if (!el) throw new Error("iv-confidence-banner not rendered");
    return el;
  }

  function textOf(root: HTMLElement, selector: string): string {
    const node = root.querySelector(selector);
    if (!node) throw new Error(`selector ${selector} matched nothing`);
    return node.textContent ?? "";
  }

  it("does not render the banner when ivConfidence is null", () => {
    setIvConfidence(null);
    expect(banner()).toBeNull();
  });

  it("renders the recorder-fallback label and confidence when iv_source=recorder", () => {
    setIvConfidence({
      ivSource: "recorder",
      latestConfidence: 0.83,
      floor: 0.5,
      gatedNow: false,
      nGated: 0,
    });

    const el = bannerOrThrow();
    expect(el.classList.contains("banner-iv-confidence-warn")).toBe(false);
    expect(textOf(el, '[data-testid="iv-source"]')).toContain("recorder fallback");
    expect(textOf(el, '[data-testid="iv-confidence-value"]')).toContain("83%");
  });

  it("flips to the warn variant and surfaces the gated-now copy when gatedNow=true", () => {
    setIvConfidence({
      ivSource: "caller_supplied",
      latestConfidence: 0.32,
      floor: 0.5,
      gatedNow: true,
      nGated: 4,
    });

    const el = bannerOrThrow();
    expect(el.classList.contains("banner-iv-confidence-warn")).toBe(true);
    expect(el.querySelector('[data-testid="iv-gated-now"]')).not.toBeNull();
    expect(textOf(el, '[data-testid="iv-n-gated"]')).toContain("4 bars");
  });

  it("uses singular bar wording when exactly one bar is gated", () => {
    setIvConfidence({
      ivSource: "recorder",
      latestConfidence: 0.6,
      floor: 0.5,
      gatedNow: false,
      nGated: 1,
    });

    const el = bannerOrThrow();
    const text = textOf(el, '[data-testid="iv-n-gated"]');
    expect(text).toContain("1 bar");
    // Make sure it's not the plural "bars" form.
    expect(text).not.toContain("bars");
  });

  it("hides the gated-now copy when confidence wasn't computed (gatedNow=null)", () => {
    setIvConfidence({
      ivSource: "absent",
      latestConfidence: null,
      floor: null,
      gatedNow: null,
      nGated: 0,
    });

    const el = bannerOrThrow();
    expect(el.querySelector('[data-testid="iv-gated-now"]')).toBeNull();
    expect(el.querySelector('[data-testid="iv-confidence-value"]')).toBeNull();
    expect(textOf(el, '[data-testid="iv-source"]')).toContain("no IV provided");
  });
});
