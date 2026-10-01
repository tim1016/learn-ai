import { provideZonelessChangeDetection } from "@angular/core";
import { render, screen } from "@testing-library/angular";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { IndicatorCategory } from "../indicator-catalog/indicator-catalog.service";
import { ChartIndicatorRailComponent } from "./chart-indicator-rail.component";

const CATEGORIES: IndicatorCategory[] = [
  {
    name: "overlap",
    indicators: [
      {
        name: "ema",
        category: "overlap",
        description: "Exponential moving average",
        configurable_params: [
          {
            name: "length",
            type: "int",
            default: 20,
            min: 2,
            max: 500,
            description: "Lookback length",
          },
        ],
      },
    ],
  },
];

async function renderRail() {
  const added: unknown[] = [];
  const view = await render(ChartIndicatorRailComponent, {
    providers: [provideZonelessChangeDetection()],
    inputs: { categories: CATEGORIES },
    on: { indicatorAdded: (entry: unknown) => added.push(entry) },
  });
  return { view, added };
}

async function openEmaConfiguration(): Promise<HTMLInputElement> {
  // The picker collapses categories, so the row is reached by opening the
  // category first and then using its Add action. The facet chip and the
  // category header share the category name, so target the header explicitly.
  const header = document.querySelector<HTMLButtonElement>(".ip-cat-head");
  if (header === null) throw new Error("category header not rendered");
  await userEvent.click(header);
  const add = [...document.querySelectorAll<HTMLButtonElement>("button.ip-btn")].find(
    (button) => button.textContent?.trim() === "Add",
  );
  if (add === undefined) {
    throw new Error(`Add action not rendered; saw: ${document.body.innerHTML.slice(0, 500)}`);
  }
  await userEvent.click(add);
  return screen.getByLabelText(/ema length/i) as HTMLInputElement;
}

describe("ChartIndicatorRailComponent parameter validation", () => {
  it("refuses a fractional value for an int parameter", async () => {
    const { view, added } = await renderRail();
    const input = await openEmaConfiguration();

    await userEvent.clear(input);
    await userEvent.type(input, "20.5");
    await view.fixture.whenStable();

    expect(screen.getByText(/whole number/i)).not.toBeNull();
    expect((screen.getByRole("button", { name: /add ema to chart/i }) as HTMLButtonElement).disabled).toBe(true);
    expect(added).toHaveLength(0);
  });

  it("refuses a value outside the advertised range", async () => {
    const { view, added } = await renderRail();
    const input = await openEmaConfiguration();

    await userEvent.clear(input);
    await userEvent.type(input, "900");
    await view.fixture.whenStable();

    expect(screen.getByText(/between 2 and 500/i)).not.toBeNull();
    expect(added).toHaveLength(0);
  });

  it("refuses an empty parameter", async () => {
    const { view, added } = await renderRail();
    const input = await openEmaConfiguration();

    await userEvent.clear(input);
    await view.fixture.whenStable();

    expect(screen.getByText(/required/i)).not.toBeNull();
    expect(added).toHaveLength(0);
  });

  it("emits once the parameter is valid again", async () => {
    const { view, added } = await renderRail();
    const input = await openEmaConfiguration();

    await userEvent.clear(input);
    await userEvent.type(input, "20.5");
    await view.fixture.whenStable();

    await userEvent.clear(input);
    await userEvent.type(input, "50");
    await view.fixture.whenStable();

    await userEvent.click(screen.getByRole("button", { name: /add ema to chart/i }));
    await view.fixture.whenStable();

    expect(added).toEqual([{ name: "ema", params: { length: 50 } }]);
  });
});
