import { provideZonelessChangeDetection } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { describe, expect, it, vi } from "vitest";

import { JobsService, type JobState } from "../../services/jobs.service";
import { JobProgressComponent } from "./job-progress.component";

function provideJobs(job: JobState | undefined) {
  return { provide: JobsService, useValue: { jobs: vi.fn().mockReturnValue(job ? [job] : []) } };
}

const RUNNING_WITH_ACKNOWLEDGMENT: JobState = {
  id: "job-1",
  type: "lean_engine_run",
  status: "running",
  phase: "parsing_results",
  startedAt: 1_753_800_000_000,
  logSeq: 0,
  eventSeq: 0,
  recentLogs: [],
  cancelAcknowledged:
    "Cancel requested — the LEAN run was already launched and will finish; its result is saved as usual.",
};

describe("JobProgressComponent", () => {
  it("renders the durable typed cancellation outcome, separate from any progress log (#2463)", async () => {
    await TestBed.configureTestingModule({
      imports: [JobProgressComponent],
      providers: [
        provideZonelessChangeDetection(),
        provideJobs({
          ...RUNNING_WITH_ACKNOWLEDGMENT,
          // Later progress noise must not overwrite the acknowledgment.
          message: "Parsing LEAN output",
        }),
      ],
    }).compileComponents();

    const fixture = TestBed.createComponent(JobProgressComponent);
    fixture.componentRef.setInput("jobId", "job-1");
    fixture.detectChanges();

    const notice = fixture.nativeElement.querySelector("div.cancel-notice[role='status']");
    expect(notice?.textContent).toContain("already launched and will finish");
  });

  it("renders no cancellation notice when no cancel was acknowledged", async () => {
    await TestBed.configureTestingModule({
      imports: [JobProgressComponent],
      providers: [
        provideZonelessChangeDetection(),
        provideJobs({
          id: "job-1",
          type: "backtest",
          status: "queued",
          startedAt: 1_753_800_000_000,
          logSeq: 0,
          eventSeq: 0,
          recentLogs: [],
        }),
      ],
    }).compileComponents();

    const fixture = TestBed.createComponent(JobProgressComponent);
    fixture.componentRef.setInput("jobId", "job-1");
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector("div.cancel-notice[role='status']")).toBeNull();
  });
});
