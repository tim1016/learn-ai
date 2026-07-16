import { fireEvent, render, screen, waitFor } from "@testing-library/angular";
import { describe, expect, it } from "vitest";

import { AccountDeskLensSelectComponent } from "./account-desk-lens-select.component";

describe("AccountDeskLensSelectComponent", () => {
  it("renders its initial signal value and emits the selected lens", async () => {
    const { fixture } = await render(AccountDeskLensSelectComponent, {
      inputs: {
        options: [
          { label: "Trader", value: "trader" },
          { label: "Operator", value: "operator" },
        ],
        value: "trader",
      },
    });

    await waitFor(() => {
      expect(
        screen
          .getByRole("button", { name: "Trader" })
          .getAttribute("aria-pressed"),
      ).toBe("true");
    });
    const operator = screen.getByRole("button", { name: "Operator" });
    expect(operator.getAttribute("aria-pressed")).toBe("false");

    fireEvent.click(operator);
    expect(fixture.componentInstance.value()).toBe("operator");
  });
});
