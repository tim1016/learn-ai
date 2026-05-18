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
  RunSummary,
  TrustedRunRequest,
  TrustedRunResponse,
} from "../../services/lean-sidecar.types";
import { LeanLabEquityChartComponent } from "./lean-lab-equity-chart/lean-lab-equity-chart.component";
import { LeanLabRunHistoryComponent } from "./lean-lab-run-history/lean-lab-run-history.component";

/** Mirror the server's ``MAX_ALGORITHM_SOURCE_BYTES``. */
const MAX_ALGORITHM_SOURCE_BYTES = 256 * 1024;

/**
 * Default placeholder shown in the "Custom algorithm" textarea so
 * the operator sees a minimal QCAlgorithm shape they can edit
 * rather than a blank box. The class name MUST be ``MyAlgorithm``
 * (LeanConfig's default ``algorithm-type-name``); a mismatch makes
 * LEAN run its image-baked default and the run looks "successful"
 * with empty output.
 */
const DEFAULT_CUSTOM_TEMPLATE = `"""Custom algorithm — Phase 4c.

Runs inside the Phase 1c hardened sandbox: read-only root, non-root
user (UID 10001 on Windows / host UID on Linux), all caps dropped,
no network, workspace-only bind mount. Algorithm output lands under
workspace/output/ and observations.csv under workspace/output/storage/.
"""

from AlgorithmImports import *


class MyAlgorithm(QCAlgorithm):
    def Initialize(self):
        start = self.GetParameter("start_date") or "2025-01-06"
        end = self.GetParameter("end_date") or "2025-01-10"
        cash = float(self.GetParameter("starting_cash") or "100000")
        sy, sm, sd = (int(x) for x in start.split("-"))
        ey, em, ed = (int(x) for x in end.split("-"))
        self.SetStartDate(sy, sm, sd)
        self.SetEndDate(ey, em, ed)
        self.SetCash(cash)
        equity = self.AddEquity("SPY", Resolution.Minute, fillForward=False)
        equity.SetDataNormalizationMode(DataNormalizationMode.Raw)
        self.SetBenchmark(lambda dt: 100)
        self.symbol = equity.Symbol

    def OnData(self, slice):
        if not self.Portfolio.Invested:
            self.SetHoldings(self.symbol, 1.0)
`;

/**
 * LEAN Lab UI — Phase 4a (form), 4b (equity chart), 4c (custom source).
 *
 * Lets an operator submit a run, watch its ``is_clean`` outcome, and
 * inspect the classified LEAN errors + normalized result without
 * leaving the UI. The "Custom algorithm" toggle (Phase 4c) sends an
 * operator-pasted QCAlgorithm to the server which executes it under
 * the Phase 1c sandbox shape (read-only root, non-root user, no caps,
 * no network, workspace-only bind mount).
 *
 * Reactive Forms (FormGroup) is the project convention; Template-
 * driven forms (ngModel) are forbidden per .claude/rules/angular.md.
 */

@Component({
  selector: "app-lean-lab",
  standalone: true,
  imports: [
    CommonModule,
    ReactiveFormsModule,
    LeanLabEquityChartComponent,
    LeanLabRunHistoryComponent,
  ],
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
    // Phase 4c — when ``useCustomAlgorithm`` is true, ``algorithmSource``
    // is sent on the request; otherwise the server runs the bundled
    // trusted sample. The textarea has a soft 256 KiB cap matching
    // the server's ``MAX_ALGORITHM_SOURCE_BYTES``.
    useCustomAlgorithm: new FormControl(false, { nonNullable: true }),
    algorithmSource: new FormControl(DEFAULT_CUSTOM_TEMPLATE, {
      nonNullable: true,
      validators: [Validators.maxLength(MAX_ALGORITHM_SOURCE_BYTES)],
    }),
    // Phase 5b — which bundled trusted sample to run when the custom
    // toggle is off. Default ``trusted_default`` matches Phase 1
    // behavior; ``reconciliation`` pins IBKR brokerage so the Phase 5a
    // fee reconciler returns a clean report.
    template: new FormControl<"trusted_default" | "reconciliation">(
      "trusted_default",
      { nonNullable: true, validators: [Validators.required] },
    ),
  });

  readonly submitting = signal(false);
  readonly response = signal<TrustedRunResponse | null>(null);
  readonly normalized = signal<NormalizedResult | null>(null);
  readonly error = signal<{ reason: string; message: string; status: number } | null>(null);

  /** Phase 4d sidebar state — populated by ``refreshRuns()``. */
  readonly runs = signal<RunSummary[]>([]);
  readonly runsTruncated = signal(false);
  readonly loadingRun = signal(false);
  /**
   * Reviewer P2 (silent catch fix): surface the index-fetch failure
   * reason in the UI so an empty sidebar isn't ambiguous (network
   * down vs. genuinely no runs). Reset on each successful refresh.
   */
  readonly runsLoadError = signal<string | null>(null);
  readonly selectedRunId = computed(() => this.response()?.run_id ?? null);

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
   * RUN_ID_PATTERN: `^[a-z0-9][a-z0-9_-]{2,63}$`.
   *
   * Seconds precision alone wasn't enough — two fast clicks within
   * the same second produced identical IDs, reusing the workspace
   * and mixing artifacts. Adding milliseconds + a short random
   * suffix removes the collision class entirely (worst case: two
   * clicks in the same millisecond with the same 5-char base-36
   * random — ~1 in 60M).
   */
  private defaultRunId(): string {
    const now = new Date();
    const ts = now.toISOString().replace(/[^0-9]/g, "").slice(0, 17); // YYYYMMDDhhmmssSSS
    const random = Math.floor(Math.random() * 36 ** 5)
      .toString(36)
      .padStart(5, "0");
    return `ui_run_${ts}_${random}`;
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

  /**
   * Phase 4d — load past runs into the sidebar. Called on init (via
   * the constructor below) and again after every successful submit so
   * the new run shows up without a page refresh.
   *
   * Failures reset the sidebar to an empty list (an empty sidebar is
   * better than the whole page erroring because the index endpoint was
   * unreachable) and are surfaced via ``runsLoadError`` so the
   * operator can see WHY the sidebar is empty. Logging to console
   * would violate the "no console.log in committed code" hard rule
   * and there's no project-wide frontend logger yet — surfacing the
   * error in the UI is the working alternative.
   */
  async refreshRuns(): Promise<void> {
    try {
      const idx = await this.service.listRuns();
      this.runs.set(idx.runs);
      this.runsTruncated.set(idx.truncated);
      this.runsLoadError.set(null);
    } catch (err) {
      this.runs.set([]);
      this.runsTruncated.set(false);
      this.runsLoadError.set(
        err instanceof LeanSidecarApiError
          ? `${err.reason}: ${err.message}`
          : err instanceof Error
            ? err.message
            : String(err),
      );
    }
  }

  /**
   * Sidebar click handler. Loads the normalized result + manifest for
   * an existing run, rehydrates the form fields (Phase 4e), and
   * renders the result panel.
   *
   * Form rehydration policy:
   * - Symbol, starting cash, and the requested window come from
   *   ``manifest.parameters`` and ``manifest.requested_window_ms``.
   * - The algorithm source is NOT rehydrated: the manifest only
   *   stores its sha256 (provenance hash), not the source itself.
   *   The toggle resets to off — operators re-running a user-source
   *   algorithm must re-paste it. The custom tag in the sidebar
   *   makes the original kind discoverable.
   * - A fresh ``runId`` is generated so a re-run with the rehydrated
   *   form lands in a NEW workspace, not the historical one (mixing
   *   artifacts in the same dir would corrupt the audit trail).
   *
   * Manifest fetch failure is non-fatal: the result panel still
   * renders (operators don't lose the click) but the form stays at
   * its previous values; a sidebar-only error pill would be
   * over-engineered for what is almost always a 404 (legacy run with
   * no manifest written).
   *
   * Reviewer P1 (Phase 4d): the synthesized ``TrustedRunResponse``
   * MUST carry the actual ``exit_code`` / ``exit_clean`` from the
   * row in ``runs()`` — synthesizing ``is_clean: true`` for every
   * historical row would paint failed runs as clean once rehydrated.
   */
  async loadRun(runId: string): Promise<void> {
    this.loadingRun.set(true);
    this.error.set(null);
    this.response.set(null);
    this.normalized.set(null);
    const summary = this.runs().find((r) => r.run_id === runId);
    try {
      const parsed = await this.service.getNormalized(runId);
      this.normalized.set(parsed);
      // Best-effort manifest fetch for form rehydration. Failure is
      // non-fatal — the result still renders, the form just isn't
      // repopulated.
      try {
        const manifest = await this.service.getManifest(runId);
        this.rehydrateFormFromManifest(manifest);
      } catch {
        // Intentional: a missing manifest (404 on legacy runs) is
        // expected and not actionable. The normalized result is
        // still on screen, which is the primary use of the click.
      }
      // Use the actual exit_code/exit_clean from the summary row.
      // Default to a "not clean, exit unknown" shape when the summary
      // isn't in the cache (e.g., the sidebar refresh raced with the
      // click) — better to under-claim than over-claim cleanliness.
      const exit_code = summary?.exit_code ?? -1;
      const is_clean = summary?.exit_clean === true;
      this.response.set({
        run_id: runId,
        is_clean,
        exit_code,
        duration_ms: 0,
        timed_out: false,
        lean_errors: { analysis_failed: [], failed_data_requests: [], runtime_error: [], other: [] },
        log_tail: "",
        manifest_path: "",
        workspace_root: "",
        observations_path: "",
        lean_log_path: "",
        normalized_path: "",
        normalized_parser_version: parsed.parser_version,
        total_order_events: parsed.total_order_events,
        total_equity_points: parsed.total_equity_points,
      });
    } catch (err) {
      if (err instanceof LeanSidecarApiError) {
        this.error.set({ reason: err.reason, message: err.message, status: err.status });
      } else {
        this.error.set({
          reason: "client_error",
          message: err instanceof Error ? err.message : String(err),
          status: 0,
        });
      }
    } finally {
      this.loadingRun.set(false);
    }
  }

  constructor() {
    void this.refreshRuns();
  }

  /**
   * Phase 4e — patch the form from a fetched manifest so the
   * operator can re-run with the same inputs (or tweak and re-run).
   * Coerces wire types defensively: starting_cash is serialized as a
   * string in the trusted-sample path but a number elsewhere, and
   * ms-since-epoch needs the inverse of ``isoDateToMsUtc``.
   *
   * Always assigns a fresh runId so re-running the form lands in a
   * new workspace, not the historical one. Custom-source runs reset
   * the toggle to off because the manifest doesn't store the source
   * (only its sha256) — operators re-pasting see the same UX as a
   * brand-new submit.
   */
  private rehydrateFormFromManifest(manifest: import("../../services/lean-sidecar.types").RunManifest): void {
    const patch: Partial<{
      symbol: string;
      startingCash: number;
      startDate: string;
      endDate: string;
      useCustomAlgorithm: boolean;
      runId: string;
    }> = {};
    const symbol = manifest.parameters?.symbol;
    if (typeof symbol === "string" && symbol.length > 0) {
      patch.symbol = symbol;
    }
    const cashRaw = manifest.parameters?.starting_cash;
    const cash = typeof cashRaw === "string" ? Number.parseFloat(cashRaw) : cashRaw;
    if (typeof cash === "number" && Number.isFinite(cash) && cash >= 1000) {
      patch.startingCash = cash;
    }
    const win = manifest.requested_window_ms;
    if (win && typeof win.start_ms === "number" && typeof win.end_ms === "number") {
      patch.startDate = this.msUtcToIsoDate(win.start_ms);
      patch.endDate = this.msUtcToIsoDate(win.end_ms);
    }
    // Manifest doesn't carry the source itself; reset the toggle off
    // and let the operator opt back in if they want to re-paste.
    patch.useCustomAlgorithm = false;
    // Re-run-ready: fresh runId so the new submit doesn't collide
    // with the historical workspace.
    patch.runId = this.defaultRunId();
    this.form.patchValue(patch);
  }

  /** Inverse of {@link isoDateToMsUtc}; ms UTC → YYYY-MM-DD. */
  private msUtcToIsoDate(ms: number): string {
    const d = new Date(ms);
    return [
      d.getUTCFullYear(),
      String(d.getUTCMonth() + 1).padStart(2, "0"),
      String(d.getUTCDate()).padStart(2, "0"),
    ].join("-");
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
    // Phase 4c — only include the algorithm_source when the toggle
    // is on AND the textarea has non-whitespace content. Sending an
    // empty string would 422 on the server's empty-check rather
    // than silently falling back to the trusted sample.
    if (value.useCustomAlgorithm && value.algorithmSource.trim()) {
      req.algorithm_source = value.algorithmSource;
    } else {
      // Phase 5b: template only matters when using a bundled sample.
      // When the operator pastes their own source, the brokerage
      // choice is whatever their source calls SetBrokerageModel with,
      // and the manifest records ``algorithm_default`` regardless.
      req.template = value.template;
    }

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
      // Refresh the sidebar so the just-submitted run appears at the
      // top, even if its normalized result fetch failed.
      void this.refreshRuns();
    }
  }
}
