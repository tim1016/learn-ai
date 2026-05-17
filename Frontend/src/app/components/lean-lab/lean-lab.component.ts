import { CommonModule } from "@angular/common";
import { ChangeDetectionStrategy, Component, computed, inject, signal } from "@angular/core";
import {
  FormControl,
  FormGroup,
  ReactiveFormsModule,
  Validators,
} from "@angular/forms";
import {
  LeanSidecarApiError,
  LeanSidecarService,
} from "../../services/lean-sidecar.service";
import type {
  NormalizedResult,
  TrustedRunRequest,
  TrustedRunResponse,
} from "../../services/lean-sidecar.types";

/**
 * Phase 4a — minimal LEAN Lab UI.
 *
 * Trusted-sample only: the form has no algorithm-source field per the
 * ADR's Phase 3 gating rule. The page lets an operator submit a
 * trusted run, watch its `is_clean` outcome, and inspect the
 * classified LEAN errors + normalized result without leaving the UI.
 *
 * Reactive Forms (FormGroup) is the project convention; Template-
 * driven forms (ngModel) are forbidden per .claude/rules/angular.md.
 */

@Component({
  selector: "app-lean-lab",
  standalone: true,
  imports: [CommonModule, ReactiveFormsModule],
  templateUrl: "./lean-lab.component.html",
  styleUrl: "./lean-lab.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LeanLabComponent {
  private readonly service = inject(LeanSidecarService);

  /**
   * Defaults match the Phase 1+2a trusted-sample window so a
   * first-time operator can click Run and see green without thinking
   * about dates. Validators mirror the server's regex + numeric
   * ranges so the form catches bad input before the round-trip.
   */
  readonly form = new FormGroup({
    runId: new FormControl(this.defaultRunId(), {
      nonNullable: true,
      validators: [
        Validators.required,
        Validators.pattern(/^[a-z0-9][a-z0-9_-]{2,63}$/),
      ],
    }),
    symbol: new FormControl("SPY", {
      nonNullable: true,
      validators: [
        Validators.required,
        Validators.pattern(/^[A-Za-z0-9.-]{1,16}$/),
      ],
    }),
    startDate: new FormControl("2025-01-06", {
      nonNullable: true,
      validators: [Validators.required],
    }),
    endDate: new FormControl("2025-01-10", {
      nonNullable: true,
      validators: [Validators.required],
    }),
    startingCash: new FormControl(100_000, {
      nonNullable: true,
      validators: [Validators.required, Validators.min(1_000), Validators.max(10_000_000)],
    }),
  });

  readonly submitting = signal(false);
  readonly response = signal<TrustedRunResponse | null>(null);
  readonly normalized = signal<NormalizedResult | null>(null);
  readonly error = signal<{ reason: string; message: string; status: number } | null>(null);

  /**
   * `is_clean` is the single boolean callers should branch on; the
   * launcher classifies LEAN's `ERROR::` lines into stable buckets
   * (analysis_failed/failed_data_requests/runtime_error/other), and
   * `is_clean` is True only when all are empty AND exit==0 AND
   * not timed out. Anything else gets a yellow warning banner.
   */
  readonly statusBadge = computed(() => {
    const r = this.response();
    if (!r) return null;
    if (r.is_clean) return { tone: "ok" as const, label: "Clean run" };
    if (r.timed_out) return { tone: "fail" as const, label: "Timed out" };
    if (r.exit_code !== 0) return { tone: "fail" as const, label: `Exit ${r.exit_code}` };
    return { tone: "warn" as const, label: "LEAN errors logged" };
  });

  /** Flatten the lean_errors buckets into [(category, line)] for display. */
  readonly errorRows = computed(() => {
    const r = this.response();
    if (!r) return [];
    const out: { category: string; line: string }[] = [];
    for (const [category, lines] of Object.entries(r.lean_errors)) {
      for (const line of lines) {
        out.push({ category, line });
      }
    }
    return out;
  });

  /**
   * For the equity-curve preview we render the first/last/sample
   * counts; a real candlestick chart lands when Phase 4b adds the
   * TradingView lightweight-charts dependency to this page.
   */
  readonly equityHighlights = computed(() => {
    const n = this.normalized();
    if (!n || n.equity_curve.length === 0) return null;
    const first = n.equity_curve[0];
    const last = n.equity_curve[n.equity_curve.length - 1];
    const pnlPct =
      first.value === 0 ? 0 : ((last.value - first.value) / first.value) * 100;
    return {
      points: n.total_equity_points,
      orders: n.total_order_events,
      start: first.value,
      end: last.value,
      pnlPct,
    };
  });

  /**
   * Regenerates on submit so the operator never accidentally
   * re-submits with the same id (which would land in the same
   * workspace dir on the server). Slug pattern matches the server's
   * RUN_ID_PATTERN.
   */
  private defaultRunId(): string {
    const ts = new Date().toISOString().replace(/[^0-9]/g, "").slice(0, 14);
    return `ui_run_${ts}`;
  }

  /**
   * Convert a YYYY-MM-DD ISO date (HTML <input type="date">) to int64
   * ms UTC at midnight UTC. The repo's timestamp-rigor rule requires
   * int64 ms UTC at every wire boundary; the date input is a UI
   * convenience converted *before* the request leaves the boundary.
   *
   * Parsed strictly to avoid `new Date("YYYY-MM-DD")` browser
   * ambiguity (Chrome parses UTC, Safari parses local — fixed via
   * explicit UTC parts).
   */
  private isoDateToMsUtc(iso: string): number {
    const [y, m, d] = iso.split("-").map((n) => Number.parseInt(n, 10));
    return Date.UTC(y, (m ?? 1) - 1, d ?? 1);
  }

  async submit(): Promise<void> {
    if (this.form.invalid) {
      this.form.markAllAsTouched();
      return;
    }
    this.submitting.set(true);
    this.error.set(null);
    this.response.set(null);
    this.normalized.set(null);

    const value = this.form.getRawValue();
    const start_ms_utc = this.isoDateToMsUtc(value.startDate);
    const end_ms_utc = this.isoDateToMsUtc(value.endDate);

    const req: TrustedRunRequest = {
      run_id: value.runId,
      symbol: value.symbol.toUpperCase(),
      start_ms_utc,
      end_ms_utc,
      starting_cash: value.startingCash,
    };

    try {
      const resp = await this.service.startTrustedRun(req);
      this.response.set(resp);
      // Refresh the runId so the next submit gets a fresh workspace.
      this.form.controls.runId.setValue(this.defaultRunId());
      // Best-effort fetch the normalized result. A run that completed
      // without parseable artifacts (LEAN crashed mid-write) returns
      // 404 here; we surface that as the "no normalized" empty state
      // rather than a hard error.
      if (resp.normalized_path) {
        try {
          const parsed = await this.service.getNormalized(resp.run_id);
          this.normalized.set(parsed);
        } catch (err) {
          if (err instanceof LeanSidecarApiError && err.status === 404) {
            this.normalized.set(null);
          } else {
            throw err;
          }
        }
      }
    } catch (err) {
      if (err instanceof LeanSidecarApiError) {
        this.error.set({
          reason: err.reason,
          message: err.message,
          status: err.status,
        });
      } else {
        this.error.set({
          reason: "client_error",
          message: err instanceof Error ? err.message : String(err),
          status: 0,
        });
      }
    } finally {
      this.submitting.set(false);
    }
  }
}
