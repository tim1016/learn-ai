import { ComponentFixture, TestBed } from "@angular/core/testing";
import { provideHttpClient } from "@angular/common/http";
import { provideHttpClientTesting } from "@angular/common/http/testing";
import { provideRouter } from "@angular/router";
import { RealizedVsIvComponent } from "./realized-vs-iv.component";
import {
  EdgeMockDataService,
  type EdgeData,
  type IvConfidenceSummary,
  type LiveIv30Marker,
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
      healthImputed: false,
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
      healthImputed: false,
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
      healthImputed: false,
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
      healthImputed: null,
    });

    const el = bannerOrThrow();
    expect(el.querySelector('[data-testid="iv-gated-now"]')).toBeNull();
    expect(el.querySelector('[data-testid="iv-confidence-value"]')).toBeNull();
    expect(textOf(el, '[data-testid="iv-source"]')).toContain("no IV provided");
  });

  // Research-doc §7.11 / §8.2.4: when the recorder row lacks an explicit
  // health_score, confidence is computed against the conservative 0.5
  // imputed prior; the UI flags this so the consumer doesn't treat the
  // confidence number as evidence-backed.

  it("renders the imputed pill when healthImputed=true", () => {
    setIvConfidence({
      ivSource: "recorder",
      latestConfidence: 0.6,
      floor: 0.5,
      gatedNow: false,
      nGated: 0,
      healthImputed: true,
    });

    const el = bannerOrThrow();
    const pill = el.querySelector('[data-testid="iv-confidence-imputed"]');
    expect(pill).not.toBeNull();
    expect(pill?.textContent).toContain("imputed");
    // Accessible name surfaces the rationale for screen-reader users.
    expect(pill?.getAttribute("aria-label")).toContain("imputed");
  });

  it("hides the imputed pill when healthImputed=false (real evidence)", () => {
    setIvConfidence({
      ivSource: "recorder",
      latestConfidence: 0.83,
      floor: 0.5,
      gatedNow: false,
      nGated: 0,
      healthImputed: false,
    });

    const el = bannerOrThrow();
    expect(el.querySelector('[data-testid="iv-confidence-imputed"]')).toBeNull();
  });

  it("hides the imputed pill when healthImputed=null (confidence not computed)", () => {
    setIvConfidence({
      ivSource: "absent",
      latestConfidence: null,
      floor: null,
      gatedNow: null,
      nGated: 0,
      healthImputed: null,
    });

    const el = bannerOrThrow();
    expect(el.querySelector('[data-testid="iv-confidence-imputed"]')).toBeNull();
  });
});

describe("RealizedVsIvComponent — live IV30 readout", () => {
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

  function setLiveIv30(live: LiveIv30Marker | null): void {
    const base = TestBed.inject(EdgeMockDataService).get();
    const next: EdgeData = { ...base, liveIv30: live };
    component.data.set(next);
    fixture.detectChanges();
  }

  it("does not render the live_iv30 readout row when liveIv30 is null", () => {
    setLiveIv30(null);
    expect(fixture.nativeElement.querySelector('[data-testid="live-iv30-readout"]')).toBeNull();
  });

  it("renders the live IV30 percent and the vix-style method label", () => {
    setLiveIv30({
      method: "vix_style",
      iv30Act365: 0.213,
      snapshotTsMs: 1_700_000_000_000,
      spot: 591,
      varianceContributionSynthetic: 0.04,
      strikeCoverageScore: 0.92,
    });

    const readout = fixture.nativeElement.querySelector('[data-testid="live-iv30-readout"]');
    if (!readout) throw new Error("live-iv30-readout not rendered");
    expect(readout.textContent).toContain("21.3%");

    const method = readout.querySelector('[data-testid="live-iv30-method"]');
    if (!method) throw new Error("live-iv30-method not rendered");
    expect(method.textContent).toContain("vix-style");

    const synth = readout.querySelector('[data-testid="live-iv30-synth"]');
    if (!synth) throw new Error("live-iv30-synth not rendered");
    expect(synth.textContent).toContain("4%");
  });

  it("labels the parametric fallback when method=parametric", () => {
    setLiveIv30({
      method: "parametric",
      iv30Act365: 0.18,
      snapshotTsMs: 1_700_000_000_000,
      spot: 591,
      varianceContributionSynthetic: 0.0,
      strikeCoverageScore: 0.5,
    });

    const method = fixture.nativeElement.querySelector('[data-testid="live-iv30-method"]');
    if (!method) throw new Error("live-iv30-method not rendered");
    expect(method.textContent).toContain("parametric");
    expect(method.textContent).not.toContain("vix-style");
  });
});
