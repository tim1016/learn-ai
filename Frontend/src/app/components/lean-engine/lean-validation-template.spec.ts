import { describe, expect, it } from "vitest";

import {
  leanValidationTemplateForStrategy,
  leanValidationTemplateLabel,
} from "./lean-validation-template";

describe("EMA Crossover 2 bps LEAN mapping", () => {
  it("maps the Strategy Lab entry to its matching trusted template", () => {
    expect(leanValidationTemplateForStrategy("ema_crossover_2_bps", "ema_crossover_2_bps"))
      .toBe("ema_crossover_2_bps");
    expect(leanValidationTemplateLabel("ema_crossover_2_bps"))
      .toBe("EMA Crossover 2 bps");
  });
});

describe("EMA Previous Crossover LEAN mapping", () => {
  it("maps the Strategy Lab entry to its matching trusted template", () => {
    expect(leanValidationTemplateForStrategy("ema_previous_crossover", "ema_previous_crossover"))
      .toBe("ema_previous_crossover");
    expect(leanValidationTemplateLabel("ema_previous_crossover"))
      .toBe("EMA Previous Crossover");
  });
});

describe("RSI Mean Reversion LEAN mapping", () => {
  it("maps the Strategy Lab entry to its matching trusted template, falls back to the strategy key when the registry omits lean_twin, and treats a null lean_twin as no LEAN validation", () => {
    expect.soft(leanValidationTemplateForStrategy("rsi_mean_reversion", "rsi_mean_reversion"), "maps the Strategy Lab entry to its matching trusted template (template)")
      .toBe("rsi_mean_reversion");
    expect.soft(leanValidationTemplateLabel("rsi_mean_reversion"), "maps the Strategy Lab entry to its matching trusted template (label)")
      .toBe("RSI Mean Reversion");
    expect.soft(leanValidationTemplateForStrategy("rsi_mean_reversion", undefined), "falls back to the strategy key when the registry omits lean_twin")
      .toBe("rsi_mean_reversion");
    expect.soft(leanValidationTemplateForStrategy("rsi_mean_reversion", null), "treats a null lean_twin as no LEAN validation, not a fallback").toBeNull();
  });
});
