import { ChangeDetectionStrategy, Component, DestroyRef, computed, inject, output, signal } from "@angular/core";
import { firstValueFrom } from "rxjs";

import { TimestampDisplayComponent } from "../../../../shared/timestamp/timestamp-display.component";
import { formatTimestampDisplay } from "../../../../shared/timestamp/timestamp-display";
import { RecencyChartService, type RecencyLaunch } from "../../../../services/recency-chart.service";

const POLL_MS = 3000;

/**
 * Recent Recency launches with their presented status and the resume gate
 * (#1938). Polls while any launch is live so a running or interrupted row
 * moves on its own, and offers Resume — one confirm-free start of a new job
 * bound to the old durable record; the run counts and the refusal copy are
 * the backend's. When a watched launch completes, `resumeCompleted` tells
 * the page to refetch the chart.
 */
@Component({
  selector: "app-recency-launches",
  imports: [TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: "./recency-launches.component.html",
  styleUrls: ["./recency-launches.component.scss"],
})
export class RecencyLaunchesComponent {
  private readonly recency = inject(RecencyChartService);
  private readonly destroyRef = inject(DestroyRef);

  readonly resumeCompleted = output();

  private readonly launchesState = signal<RecencyLaunch[]>([]);
  readonly launches = this.launchesState.asReadonly();
  readonly loadFailed = signal(false);
  readonly resumeFailed = signal(false);
  private readonly resumingId = signal<string | null>(null);

  readonly hasLaunches = computed(() => this.launches().length > 0);

  private readonly pollTimer = setInterval(() => {
    void this.reload();
  }, POLL_MS);

  constructor() {
    this.destroyRef.onDestroy(() => clearInterval(this.pollTimer));
    void this.reload();
  }

  isResuming(launch: RecencyLaunch): boolean {
    return this.resumingId() === launch.launchId;
  }

  runsLabel(launch: RecencyLaunch): string {
    return launch.failedRuns > 0 ? `${launch.succeededRuns}/${launch.expectedRuns} · ${launch.failedRuns} failed` : `${launch.succeededRuns}/${launch.expectedRuns}`;
  }

  resumeAriaDate(launch: RecencyLaunch): string {
    return formatTimestampDisplay(launch.createdAtMs, { mode: "local" });
  }

  async reload(): Promise<void> {
    let next: RecencyLaunch[];
    try {
      next = await firstValueFrom(this.recency.launches());
    } catch {
      this.loadFailed.set(true);
      return;
    }
    const wasLive = new Set(this.launchesState().filter(isLive).map((l) => l.launchId));
    this.loadFailed.set(false);
    this.launchesState.set(next);
    if (next.some((l) => wasLive.has(l.launchId) && l.status === "completed")) {
      this.resumeCompleted.emit();
    }
  }

  async resume(launch: RecencyLaunch): Promise<void> {
    if (!launch.resumable || this.resumingId() !== null) return;
    this.resumingId.set(launch.launchId);
    this.resumeFailed.set(false);
    try {
      await this.recency.resume(launch);
      await this.reload();
    } catch {
      this.resumeFailed.set(true);
    } finally {
      this.resumingId.set(null);
    }
  }
}

function isLive(launch: RecencyLaunch): boolean {
  return launch.status === "running" || launch.status === "interrupted";
}
