import { Component } from "@angular/core";
import { fireEvent, render, screen } from "@testing-library/angular";
import { describe, expect, it } from "vitest";

import { ParamRangeInputComponent } from "./param-range-input.component";
import type { ParamRange } from "./param-range";

async function renderInput(range: ParamRange) {
  return render(ParamRangeInputComponent, {
    inputs: { paramName: "gap_bps", title: "Crossover gap (bps)", defaultValue: 2, range },
  });
}

describe("ParamRangeInputComponent", () => {
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

  it("keeps a malformed list in the field, names the entry, and refuses it as an empty list", async () => {
    // #1940: ".3..4" parsed as NaN and was dropped, so the model shrank to
    // [0, 0.1, 0.2, 0.5] with no message and Launch stayed enabled.
    const view = await renderInput({ type: "value_list", values: [2] });

    fireEvent.input(screen.getByLabelText(/values/i), { target: { value: "0,.1,.2,.3..4,.5" } });
    await view.fixture.whenStable();

    const field = screen.getByLabelText(/values/i) as HTMLInputElement;
    expect(field.value).toBe("0,.1,.2,.3..4,.5");
    expect(field.getAttribute("aria-invalid")).toBe("true");
    expect(screen.getByRole("alert").textContent).toContain('".3..4" is not a number.');
    expect(view.fixture.componentInstance.range()).toEqual({ type: "value_list", values: [] });
  });

  it("refuses an emptied list instead of reading it as a single zero", async () => {
    const view = await renderInput({ type: "value_list", values: [2] });

    fireEvent.input(screen.getByLabelText(/values/i), { target: { value: "" } });
    await view.fixture.whenStable();

    expect(screen.getByRole("alert").textContent).toContain("Enter at least one value.");
    expect(view.fixture.componentInstance.range()).toEqual({ type: "value_list", values: [] });
  });

  it("clears the refusal once the list parses again", async () => {
    const view = await renderInput({ type: "value_list", values: [2] });

    fireEvent.input(screen.getByLabelText(/values/i), { target: { value: "1,x" } });
    await view.fixture.whenStable();
    fireEvent.input(screen.getByLabelText(/values/i), { target: { value: "1, 2" } });
    await view.fixture.whenStable();

    expect(screen.queryByRole("alert")).toBeNull();
    expect((screen.getByLabelText(/values/i) as HTMLInputElement).getAttribute("aria-invalid")).toBeNull();
    expect(view.fixture.componentInstance.range()).toEqual({ type: "value_list", values: [1, 2] });
  });

  it("shows the parent's new range and drops the refusal when the range is written from outside", async () => {
    const view = await renderInput({ type: "value_list", values: [2] });
    fireEvent.input(screen.getByLabelText(/values/i), { target: { value: "1,x" } });
    await view.fixture.whenStable();
    expect(screen.getByRole("alert")).not.toBeNull();

    view.fixture.componentRef.setInput("range", { type: "value_list", values: [5, 10] });
    await view.fixture.whenStable();

    expect((screen.getByLabelText(/values/i) as HTMLInputElement).value).toBe("5, 10");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("gives each instance its own element ids, so same-named parameters of two strategies do not collide", async () => {
    // Codex review on #1982: two selected Recency strategies exposing the same
    // parameter name rendered the same field and problem ids.
    @Component({
      imports: [ParamRangeInputComponent],
      template: `
        <app-param-range-input paramName="rsi_period" [range]="first" />
        <app-param-range-input paramName="rsi_period" [range]="second" />
      `,
    })
    class TwoEditorsHost {
      first: ParamRange = { type: "value_list", values: [14] };
      second: ParamRange = { type: "value_list", values: [21] };
    }
    const view = await render(TwoEditorsHost);

    const inputs = Array.from(view.container.querySelectorAll<HTMLInputElement>("input[type=text]"));
    expect(inputs).toHaveLength(2);
    expect(new Set(inputs.map((el) => el.id)).size).toBe(2);
    expect(screen.getAllByLabelText(/values/i).map((el) => el.id)).toEqual(inputs.map((el) => el.id));
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
