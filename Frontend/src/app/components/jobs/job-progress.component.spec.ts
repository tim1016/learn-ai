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
  logSeq: 2,
  eventSeq: 0,
  recentLogs: [
    { level: "info", message: "Staging LEAN fixtures…", ts: 1, seq: 1 },
    {
      level: "info",
      message:
        "Cancel requested — the LEAN run was already launched and will finish; its result is saved as usual.",
      ts: 2,
      seq: 2,
    },
  ],
};

describe("JobProgressComponent", () => {
  it("surfaces the worker's latest log line so a too-late cancel is acknowledged in the drawer (#2463)", async () => {
    await TestBed.configureTestingModule({
      imports: [JobProgressComponent],
      providers: [provideZonelessChangeDetection(), provideJobs(RUNNING_WITH_ACKNOWLEDGMENT)],
    }).compileComponents();

    const fixture = TestBed.createComponent(JobProgressComponent);
    fixture.componentRef.setInput("jobId", "job-1");
    fixture.detectChanges();

    const status = fixture.nativeElement.querySelector("div.log[role='status']");
    expect(status?.textContent).toContain("already launched and will finish");
  });

  it("renders no log line when the worker has said nothing", async () => {
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

    expect(fixture.nativeElement.querySelector("div.log[role='status']")).toBeNull();
  });
});
