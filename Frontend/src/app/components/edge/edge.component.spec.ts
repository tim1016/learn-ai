import { provideHttpClient } from "@angular/common/http";
import { HttpTestingController, provideHttpClientTesting } from "@angular/common/http/testing";
import { TestBed } from "@angular/core/testing";
import { provideRouter } from "@angular/router";
import { RouterTestingHarness } from "@angular/router/testing";
import { screen, within } from "@testing-library/angular";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { routes } from "../../app.routes";
import { JobsService } from "../../services/jobs.service";
import { fakePickerWorld } from "../../shared/symbol-picker/testing/fake-picker-world";
import { answerCompute, FakeJobs } from "./testing/edge-compute-world";

describe("EdgeComponent — sample labeling", () => {
  let harness: RouterTestingHarness;
  let jobs: FakeJobs;

  beforeEach(async () => {
    jobs = new FakeJobs();
    TestBed.configureTestingModule({
      providers: [
        { provide: JobsService, useValue: jobs },
        provideRouter(routes),
        provideHttpClient(),
        provideHttpClientTesting(),
        ...fakePickerWorld().providers,
      ],
    });
    harness = await RouterTestingHarness.create();
  });

  it.each([
    ["/edge", "Edge Analysis"],
    ["/edge/realized-vs-iv", "Realized vs IV"],
    ["/edge/cross-asset", "Cross-Asset"],
    ["/edge/regimes", "Regimes"],
  ])("heads %s with its menu label, not one shared heading", async (url, label) => {
    await harness.navigateByUrl(url);

    expect(screen.getByRole("heading", { level: 1, name: label })).toBeTruthy();
  });

  it("labels the hub's headline numbers as sample data", async () => {
    await harness.navigateByUrl("/edge");

    expect(screen.getAllByTestId("sample-data-banner")).toHaveLength(1);
    expect(screen.getByText("Edge composite")).toBeTruthy();
  });

  it("labels each drawer opened from the hub as sample data", async () => {
    const user = userEvent.setup();
    await harness.navigateByUrl("/edge");

    await user.click(screen.getByRole("button", { name: "Open Edge Score inspector" }));
    const inspector = screen.getByRole("dialog", { name: "Edge Score Inspector" });
    expect(within(inspector).getByTestId("sample-data-banner")).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "Close inspector" }));
    await user.click(screen.getByRole("button", { name: "Open Trade Simulator" }));
    const simulator = screen.getByRole("dialog", { name: "Trade Simulator" });
    expect(within(simulator).getByTestId("sample-data-banner")).toBeTruthy();
  });

  it("keeps the label on a drawer that sits beside a real study, though the study itself carries none", async () => {
    const user = userEvent.setup();
    await harness.navigateByUrl("/edge/realized-vs-iv");
    await user.click(screen.getByRole("button", { name: "Compute" }));
    await answerCompute({ http: TestBed.inject(HttpTestingController), jobs });
    harness.detectChanges();
    expect(screen.getByTestId("rviv-source-tag"), "the real study is on screen").toBeTruthy();
    expect(screen.queryByTestId("sample-data-banner"), "nothing on the study page is synthetic").toBeNull();

    await user.click(screen.getByRole("button", { name: "Open Edge Score inspector" }));

    const inspector = screen.getByRole("dialog", { name: "Edge Score Inspector" });
    expect(within(inspector).getByTestId("sample-data-banner")).toBeTruthy();
    expect(screen.getAllByTestId("sample-data-banner")).toHaveLength(1);
  });
});
