import { CommonModule } from "@angular/common";
import { ChangeDetectionStrategy, Component, computed, input, output } from "@angular/core";
import type { RunSummary } from "../../../services/lean-sidecar.types";

/**
 * Phase 4d sidebar — lists past runs from the artifacts root and lets
 * the operator click one to rehydrate it in the main panel.
 *
 * Pure presentational component: the parent owns the run list signal
 * (refreshing it after each submit) and reacts to the ``runSelected``
 * output by calling the normalized endpoint and updating its state.
 * Keeping rehydration logic in the parent avoids duplicating it with
 * the new-run flow.
 */
@Component({
  selector: "app-lean-lab-run-history",
  standalone: true,
  imports: [CommonModule],
  templateUrl: "./lean-lab-run-history.component.html",
  styleUrl: "./lean-lab-run-history.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LeanLabRunHistoryComponent {
  readonly runs = input.required<RunSummary[]>();
  readonly selectedRunId = input<string | null>(null);
  /** True when the parent is mid-load — disables the click handler. */
  readonly loading = input<boolean>(false);
  /** Server cap reached — the list omits older runs. */
  readonly truncated = input<boolean>(false);
  /**
   * Index-endpoint failure reason (from the parent's ``refreshRuns``).
   * When present, the sidebar shows a "couldn't load runs" line so
   * an empty list is not ambiguous with a fetch error.
   */
  readonly loadError = input<string | null>(null);

  readonly runSelected = output<string>();

  readonly hasRuns = computed(() => this.runs().length > 0);

  /**
   * Format the started_at timestamp for the sidebar row. Per the
   * timestamp-rigor rule, the only display-side TZ conversion lives
   * here at the UI boundary; the wire format stays ``int64 ms UTC``.
   */
  formatStartedAt(ms: number | null): string {
    if (ms === null) return "—";
    return new Date(ms).toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  /**
   * Status pill copy + tone. ``exit_clean=true`` is green; ``false``
   * is red; ``null`` (no finished_at_ms — likely still running or
   * crashed before manifest write) is grey.
   */
  statusFor(run: RunSummary): { tone: "ok" | "fail" | "unknown"; label: string } {
    if (run.exit_clean === true) return { tone: "ok", label: "exit 0" };
    if (run.exit_clean === false) return { tone: "fail", label: `exit ${run.exit_code}` };
    return { tone: "unknown", label: "no manifest" };
  }

  onClick(runId: string): void {
    if (this.loading()) return;
    this.runSelected.emit(runId);
  }
}
