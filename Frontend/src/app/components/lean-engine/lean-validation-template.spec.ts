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
