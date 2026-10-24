import { render, screen, within } from "@testing-library/angular";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { CrossAssetComponent } from "./cross-asset.component";

describe("CrossAssetComponent — sample labeling", () => {
  it("opens labeled as sample data", async () => {
    await render(CrossAssetComponent);

    expect(screen.getByTestId("sample-data-banner").textContent).toContain("Sample data — not real");
  });

  it("keeps the label while the composite view changes what is drawn", async () => {
    const user = userEvent.setup();
    await render(CrossAssetComponent);

    await user.click(screen.getByRole("button", { name: "Equal-weight composite" }));

    expect(screen.getAllByTestId("sample-data-banner")).toHaveLength(1);
  });

  it("offers no split or vol-weighted switch, since the sample draws neither", async () => {
    await render(CrossAssetComponent);

    for (const name of ["rolling", "calendar", "walk-forward", "Vol-weighted parity"]) {
      expect(screen.queryByRole("button", { name }), name).toBeNull();
    }
  });

  it("labels each drawer it opens as sample data too", async () => {
    const user = userEvent.setup();
    await render(CrossAssetComponent);

    await user.click(screen.getByRole("button", { name: /edge_score/ }));
    const inspector = screen.getByRole("dialog", { name: "Edge Score Inspector" });
    expect(within(inspector).getByTestId("sample-data-banner")).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "Close inspector" }));
    await user.click(screen.getByRole("button", { name: /Run trade simulator/ }));
    const simulator = screen.getByRole("dialog", { name: "Trade Simulator" });
    expect(within(simulator).getByTestId("sample-data-banner")).toBeTruthy();
  });
});
