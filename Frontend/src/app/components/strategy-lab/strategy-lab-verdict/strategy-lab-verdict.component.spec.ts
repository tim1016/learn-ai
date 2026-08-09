import { provideZonelessChangeDetection } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { describe, expect, it } from "vitest";

import type { RunVerdict } from "../../../api/run-verdict.types";
import { StrategyLabVerdictComponent } from "./strategy-lab-verdict.component";

const VERDICT: RunVerdict = {
  verdict_version: 1,
  engine: "python",
  generated_at_ms: 1,
  composite: 72,
  grade: "B",
  signal: "Iterate",
  headline: "Not duplicated in the slim line",
  red_flags: [],
  dimensions: [],
  missing_metrics: [],
  normalized_weights: false,
  cleanliness: null,
};

async function render(status: string) {
  TestBed.resetTestingModule();
  await TestBed.configureTestingModule({
    imports: [StrategyLabVerdictComponent],
    providers: [provideZonelessChangeDetection()],
  }).compileComponents();
  const fixture = TestBed.createComponent(StrategyLabVerdictComponent);
  fixture.componentRef.setInput("verdict", VERDICT);
  fixture.componentRef.setInput("tradeCount", 8);
  fixture.componentRef.setInput("parity", {
    status,
    reason: null,
    countsByCategory: [{ category: "FILL_PRICE_DRIFT", count: 2 }],
    divergences: [{ category: "FILL_PRICE_DRIFT", message: "Two fills differ" }],
  });
  fixture.detectChanges();
  return fixture;
}

describe("StrategyLabVerdictComponent", () => {
  it("renders one diagnostic line without restating configuration", async () => {
    const fixture = await render("agree");
    const root = fixture.nativeElement as HTMLElement;
    expect(root.textContent).toContain("B · 72");
    expect(root.textContent).toContain("Diagnostic complete");
    expect(root.textContent).toContain("8 trades");
    expect(root.textContent).toContain("parity: Agree");
    expect(root.querySelector(".verdict-line__breakdown")).toBeNull();
    expect(root.textContent).not.toContain("Not duplicated in the slim line");
  });

  it("makes parity expandable only when the engines diverged", async () => {
    const fixture = await render("diverged");
    const root = fixture.nativeElement as HTMLElement;
    const button = root.querySelector<HTMLButtonElement>(".verdict-line__parity");
    expect(button).not.toBeNull();
    button?.click();
    fixture.detectChanges();
    expect(root.textContent).toContain("Fill Price Drift");
    expect(root.textContent).toContain("Two fills differ");
  });
});
