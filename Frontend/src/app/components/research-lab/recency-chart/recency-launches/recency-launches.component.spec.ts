import { render, screen, waitFor } from "@testing-library/angular";
import { userEvent } from "@testing-library/user-event";
import { of, throwError } from "rxjs";
import { describe, expect, it, vi } from "vitest";

import { RecencyLaunchesComponent } from "./recency-launches.component";
import { RecencyChartService, type RecencyLaunch } from "../../../../services/recency-chart.service";

function makeLaunch(overrides: Partial<RecencyLaunch> = {}): RecencyLaunch {
  return {
    launchId: "launch-1",
    status: "interrupted",
    attempt: 1,
    expectedRuns: 4,
    succeededRuns: 2,
    failedRuns: 0,
    createdAtMs: Date.parse("2026-09-20T10:00:00Z"),
    completedAtMs: null,
    resumable: true,
    resumeRefusal: null,
    request: { symbols: ["SPY"] },
    ...overrides,
  };
}

async function renderLaunches(launches: RecencyLaunch[], resume = vi.fn(async () => "job-2")) {
  const view = await render(RecencyLaunchesComponent, {
    providers: [{ provide: RecencyChartService, useValue: { launches: () => of(launches), resume } }],
  });
  return { view, resume };
}

describe("RecencyLaunchesComponent", () => {
  it("lists launches with their presented status and run counts", async () => {
    await renderLaunches([
      makeLaunch(),
      makeLaunch({ launchId: "launch-2", status: "completed", succeededRuns: 4, completedAtMs: Date.parse("2026-09-20T11:00:00Z"), resumable: false, resumeRefusal: "the Recency launch is complete" }),
    ]);

    expect(screen.getByText("interrupted")).not.toBeNull();
    expect(screen.getByText("completed")).not.toBeNull();
    expect(screen.getByText("2/4")).not.toBeNull();
    expect(screen.getByText("4/4")).not.toBeNull();
  });

  it("offers Resume only for a resumable launch and starts it with the stored request", async () => {
    const { resume } = await renderLaunches([
      makeLaunch(),
      makeLaunch({ launchId: "launch-2", status: "completed", resumable: false, resumeRefusal: "the Recency launch is complete" }),
    ]);
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: /resume launch from/i }));

    expect(resume).toHaveBeenCalledWith(makeLaunch());
    expect(screen.queryByText("the Recency launch is complete")).not.toBeNull();
    await waitFor(() => expect(screen.queryByRole("button", { name: /resume launch from/i })).not.toBeNull());
  });

  it("shows the refusal copy instead of a button for a completed launch", async () => {
    await renderLaunches([makeLaunch({ status: "completed", resumable: false, resumeRefusal: "the Recency launch is complete" })]);

    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText("the Recency launch is complete")).not.toBeNull();
  });

  it("explains itself when the load fails", async () => {
    await render(RecencyLaunchesComponent, {
      providers: [{ provide: RecencyChartService, useValue: { launches: () => throwError(() => new Error("down")), resume: vi.fn() } }],
    });

    expect(screen.getByText(/could not load the recent launches/i)).not.toBeNull();
  });
});
