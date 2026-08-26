import { provideZonelessChangeDetection } from "@angular/core";
import { render, screen } from "@testing-library/angular";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  LeanSourceService,
  type LeanStrategySource,
} from "../../../services/lean-source.service";
import { LeanSourceEditorComponent } from "./lean-source-editor.component";

const REGISTERED_SOURCE: LeanStrategySource = {
  strategy_name: "ema_crossover_signal",
  template: "ema_crossover_signal",
  language: "python",
  source: "from AlgorithmImports import *\n\nclass MyAlgorithm(QCAlgorithm):\n    pass\n",
  source_sha256: "a".repeat(64),
};

const SECOND_REGISTERED_SOURCE: LeanStrategySource = {
  strategy_name: "rsi_mean_reversion_signal",
  template: "rsi_mean_reversion_signal",
  language: "python",
  source: "from AlgorithmImports import *\n\nclass SecondAlgorithm(QCAlgorithm):\n    pass\n",
  source_sha256: "b".repeat(64),
};

let rangeClientRectsDescriptor: PropertyDescriptor | undefined;

beforeEach(() => {
  // CodeMirror measures edited ranges; jsdom does not implement this layout API.
  rangeClientRectsDescriptor = Object.getOwnPropertyDescriptor(Range.prototype, "getClientRects");
  Object.defineProperty(Range.prototype, "getClientRects", {
    configurable: true,
    value: () => [],
  });
});

afterEach(() => {
  if (rangeClientRectsDescriptor) {
    Object.defineProperty(Range.prototype, "getClientRects", rangeClientRectsDescriptor);
  } else {
    Reflect.deleteProperty(Range.prototype, "getClientRects");
  }
});

describe("LeanSourceEditorComponent", () => {
  it("shows the QCAlgorithm without treating an undetected runtime as an error", async () => {
    const getStrategySource = vi.fn(async () => REGISTERED_SOURCE);
    const result = await render(LeanSourceEditorComponent, {
      inputs: {
        strategyName: REGISTERED_SOURCE.strategy_name,
        launcherStatus: "unknown",
      },
      providers: [
        provideZonelessChangeDetection(),
        { provide: LeanSourceService, useValue: { getStrategySource } },
      ],
    });

    expect(await screen.findByText(/LEAN runtime not detected/i)).not.toBeNull();
    await vi.waitFor(() => {
      expect(result.container.textContent).toContain("class MyAlgorithm");
    });
    expect(screen.queryByRole("alert")).toBeNull();
    expect(getStrategySource).toHaveBeenCalledWith("ema_crossover_signal");
  });

  it("keeps custom editing available before the runtime is detected", async () => {
    const user = userEvent.setup();
    await render(LeanSourceEditorComponent, {
      inputs: {
        strategyName: REGISTERED_SOURCE.strategy_name,
        launcherStatus: "blocked",
      },
      providers: [
        provideZonelessChangeDetection(),
        { provide: LeanSourceService, useValue: { getStrategySource: async () => REGISTERED_SOURCE } },
      ],
    });
    const toggle = await screen.findByRole("checkbox", { name: /Use custom source/i });
    if (!(toggle instanceof HTMLInputElement)) throw new Error("Custom-source toggle is not an input");

    await user.click(toggle);

    expect(toggle.checked).toBe(true);
    const editor = screen.getByLabelText("QCAlgorithm source editor");
    expect(editor.getAttribute("contenteditable")).toBe("true");
    await user.type(editor, "# browser edit");
    expect(editor.textContent).toContain("# browser edit");
    const reset = screen.getByRole("button", { name: "Reset" });
    if (!(reset instanceof HTMLButtonElement)) throw new Error("Reset action is not a button");
    expect(reset.disabled).toBe(false);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("does not restore the previous strategy source when undoing after a strategy switch", async () => {
    const user = userEvent.setup();
    const getStrategySource = vi.fn(async (strategyName: string) =>
      strategyName === SECOND_REGISTERED_SOURCE.strategy_name
        ? SECOND_REGISTERED_SOURCE
        : REGISTERED_SOURCE,
    );
    const { rerender } = await render(LeanSourceEditorComponent, {
      inputs: {
        strategyName: REGISTERED_SOURCE.strategy_name,
        launcherStatus: "ready",
      },
      providers: [
        provideZonelessChangeDetection(),
        { provide: LeanSourceService, useValue: { getStrategySource } },
      ],
    });
    const toggle = await screen.findByRole("checkbox", { name: /Use custom source/i });
    await user.click(toggle);
    const editor = screen.getByLabelText("QCAlgorithm source editor");
    await user.click(editor);
    await user.type(editor, "# strategy A edit");
    expect(editor.textContent).toContain("strategy A edit");

    await rerender({
      componentInputs: {
        strategyName: SECOND_REGISTERED_SOURCE.strategy_name,
        launcherStatus: "ready",
        customSource: null,
      },
    });
    await screen.findByText(SECOND_REGISTERED_SOURCE.template);
    await vi.waitFor(() => {
      expect(editor.textContent).toContain("SecondAlgorithm");
    });
    if (!(toggle instanceof HTMLInputElement)) throw new Error("Custom-source toggle is not an input");
    if (!toggle.checked) await user.click(toggle);

    await user.click(editor);
    await user.keyboard("{Control>}z{/Control}");

    expect(editor.textContent).toContain("SecondAlgorithm");
    expect(editor.textContent).not.toContain("MyAlgorithm");
    expect(editor.textContent).not.toContain("strategy A edit");
  });
});
