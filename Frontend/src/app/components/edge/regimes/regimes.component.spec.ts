import { render, screen, within } from "@testing-library/angular";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { RegimesComponent } from "./regimes.component";

describe("RegimesComponent — sample labeling", () => {
  it("opens labeled as sample data", async () => {
    await render(RegimesComponent);

    expect(screen.getByTestId("sample-data-banner").textContent).toContain("Sample data — not real");
  });

  it("keeps the label while the decoding view changes what is drawn", async () => {
    const user = userEvent.setup();
    await render(RegimesComponent);

    await user.click(screen.getByRole("button", { name: "posterior" }));

    expect(screen.getAllByTestId("sample-data-banner")).toHaveLength(1);
  });

  it("offers no algorithm switch, since the sample draws one fixed three-state path", async () => {
    await render(RegimesComponent);

    for (const name of ["hmm", "kmeans", "compare"]) {
      expect(screen.queryByRole("button", { name }), name).toBeNull();
    }
  });

  it("labels the Edge score drawer it opens as sample data too", async () => {
    const user = userEvent.setup();
    await render(RegimesComponent);

    await user.click(screen.getByRole("button", { name: /edge_score/ }));

    const inspector = screen.getByRole("dialog", { name: "Edge Score Inspector" });
    expect(within(inspector).getByTestId("sample-data-banner")).toBeTruthy();
  });
});
