import { fireEvent, render, screen } from "@testing-library/angular";
import { describe, expect, it } from "vitest";

import { RecencyParamRangeInputComponent } from "./recency-param-range-input.component";
import type { ParamRange } from "./recency-param-range";

async function renderInput(range: ParamRange) {
  return render(RecencyParamRangeInputComponent, {
    inputs: { paramName: "gap_bps", title: "Crossover gap (bps)", defaultValue: 2, range },
  });
}

describe("RecencyParamRangeInputComponent", () => {
  it("renders the field title", async () => {
    await renderInput({ type: "value_list", values: [2] });
    expect(screen.getByText("Crossover gap (bps)")).not.toBeNull();
  });

  it("shows the value-list mode's values as a comma-joined string", async () => {
    await renderInput({ type: "value_list", values: [1, 2, 3] });
    const listInput = screen.getByLabelText(/values/i) as HTMLInputElement;
    expect(listInput.value).toBe("1, 2, 3");
  });

  it("updates the model to a value-list when the values field changes", async () => {
    const view = await renderInput({ type: "value_list", values: [2] });

    fireEvent.input(screen.getByLabelText(/values/i), { target: { value: "1, 2, 3" } });
    await view.fixture.whenStable();

    expect(view.fixture.componentInstance.range()).toEqual({ type: "value_list", values: [1, 2, 3] });
  });

  it("switches to low/high/step mode and updates the model", async () => {
    const view = await renderInput({ type: "value_list", values: [2] });

    fireEvent.click(screen.getByRole("button", { name: /range/i }));
    fireEvent.input(screen.getByLabelText(/^low$/i), { target: { value: "1" } });
    fireEvent.input(screen.getByLabelText(/^high$/i), { target: { value: "5" } });
    fireEvent.input(screen.getByLabelText(/^step$/i), { target: { value: "0.5" } });
    await view.fixture.whenStable();

    expect(view.fixture.componentInstance.range()).toEqual({ type: "low_high_step", low: 1, high: 5, step: 0.5 });
  });
});
