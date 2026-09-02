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

describe("RSI Mean Reversion LEAN mapping", () => {
  it("maps the Strategy Lab entry to its matching trusted template", () => {
    expect(leanValidationTemplateForStrategy("rsi_mean_reversion", "rsi_mean_reversion"))
      .toBe("rsi_mean_reversion");
    expect(leanValidationTemplateLabel("rsi_mean_reversion"))
      .toBe("RSI Mean Reversion");
  });

  it("falls back to the strategy key when the registry omits lean_twin", () => {
    expect(leanValidationTemplateForStrategy("rsi_mean_reversion", undefined))
      .toBe("rsi_mean_reversion");
  });

  it("treats a null lean_twin as no LEAN validation, not a fallback", () => {
    expect(leanValidationTemplateForStrategy("rsi_mean_reversion", null)).toBeNull();
  });
});
