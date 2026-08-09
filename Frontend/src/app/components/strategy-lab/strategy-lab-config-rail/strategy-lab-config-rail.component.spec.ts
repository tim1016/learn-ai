import { provideZonelessChangeDetection } from "@angular/core";
import { ComponentFixture, TestBed } from "@angular/core/testing";
import { describe, expect, it } from "vitest";

import type { StrategyInfo } from "../../lean-engine/lean-engine.component";
import { StrategyLabConfigRailComponent } from "./strategy-lab-config-rail.component";

const STRATEGY: StrategyInfo = {
  name: "ema_crossover_signal",
  display_name: "EMA crossover",
  description: "EMA signal",
  supported_resolutions: ["minute"],
  params_schema: {
    properties: {
      symbol: { type: "string", default: "SPY" },
      fast: { type: "integer", default: 12, minimum: 2 },
      slow: { type: "integer", default: 26, minimum: 3 },
    },
  },
};

async function createRail(collapsed = false): Promise<ComponentFixture<StrategyLabConfigRailComponent>> {
  TestBed.resetTestingModule();
  await TestBed.configureTestingModule({
    imports: [StrategyLabConfigRailComponent],
    providers: [provideZonelessChangeDetection()],
  }).compileComponents();
  const fixture = TestBed.createComponent(StrategyLabConfigRailComponent);
  fixture.componentRef.setInput("collapsed", collapsed);
  fixture.componentRef.setInput("engine", "both");
  fixture.componentRef.setInput("range", {
    symbol: "SPY",
    from: "2026-01-05",
    to: "2026-01-30",
    resolution: "minute",
    autoFetch: true,
  });
  fixture.componentRef.setInput("strategies", [STRATEGY]);
  fixture.componentRef.setInput("selectedStrategyName", STRATEGY.name);
  fixture.componentRef.setInput("paramValues", { fast: 12, slow: 26 });
  fixture.detectChanges();
  return fixture;
}

describe("StrategyLabConfigRailComponent", () => {
  it("renders exactly the four primary flat inputs and keeps parameters in Advanced", async () => {
    const fixture = await createRail();
    const root = fixture.nativeElement as HTMLElement;

    expect(root.textContent).toContain("Engine");
    expect(root.textContent).toContain("Instrument");
    expect(root.textContent).toContain("Time window");
    expect(root.textContent).toContain("Strategy");
    expect(root.querySelectorAll("app-config-section")).toHaveLength(0);
    expect(root.querySelector(".availability-strip")).toBeNull();
    expect(root.querySelector(".availability-legend")).toBeNull();

    const advanced = root.querySelector<HTMLDetailsElement>("details.advanced");
    expect(advanced?.open).toBe(false);
    if (!advanced) throw new Error("Advanced disclosure is missing");
    advanced.open = true;
    fixture.detectChanges();
    expect(advanced?.textContent).toContain("1 minute · read only");
    expect(advanced?.textContent).toContain("Strategy parameters");
    expect(advanced?.textContent).toContain("Execution");
    expect(advanced?.textContent).toContain("LEAN launcher");
  });

  it("renders only the recall icon strip when collapsed", async () => {
    const fixture = await createRail(true);
    const root = fixture.nativeElement as HTMLElement;

    expect(root.querySelector(".config-rail")).toBeNull();
    expect(root.querySelector(".config-strip")).not.toBeNull();
    expect(root.querySelectorAll(".config-strip button")).toHaveLength(5);
    expect(root.querySelector<HTMLButtonElement>("[aria-label='Expand configuration']")).not.toBeNull();
  });
});
