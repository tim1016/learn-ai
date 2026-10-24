import { render, screen, within } from "@testing-library/angular";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { RegimesComponent } from "./regimes.component";

describe("RegimesComponent — sample labeling", () => {
  it("opens labeled as sample data", async () => {
    await render(RegimesComponent);

    expect(screen.getByTestId("sample-data-banner").textContent).toContain("Sample data — not real");
  });

  it("keeps the label while the algorithm and decoding views change what is drawn", async () => {
    const user = userEvent.setup();
    await render(RegimesComponent);

    await user.click(screen.getByRole("button", { name: "kmeans" }));
    await user.click(screen.getByRole("button", { name: "posterior" }));

    expect(screen.getAllByTestId("sample-data-banner")).toHaveLength(1);
  });

  it("labels the Edge score drawer it opens as sample data too", async () => {
    const user = userEvent.setup();
    await render(RegimesComponent);

    await user.click(screen.getByRole("button", { name: /edge_score/ }));

    const inspector = screen.getByRole("dialog", { name: "Edge Score Inspector" });
    expect(within(inspector).getByTestId("sample-data-banner")).toBeTruthy();
  });
});
