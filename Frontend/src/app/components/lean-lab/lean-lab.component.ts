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
  CrossEngineReconciliationReport,
  NormalizedResult,
  RunReconciliationReport,
  RunSummary,
  TrustedRunRequest,
  TrustedRunResponse,
} from "../../services/lean-sidecar.types";
import { CROSS_RECONCILE_SCHEMA_VERSION } from "../../services/lean-sidecar.types";
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

// Phase 4f: rehydrate the LeanErrorBuckets dict from a list of
// category names. The manifest stores only category NAMES (not the
// individual ERROR:: lines), so each hit category gets a single
// placeholder line that names the manifest as the source of the
// information. Categories not present in the manifest get an empty
// list, matching the launcher's "no error in this bucket" semantics.
const _MANIFEST_PLACEHOLDER_LINE =
  "(line content not in manifest — fetch /runs/{id}/log for details)";

function rehydratedLeanErrors(categories: string[]): {
  analysis_failed: string[];
  failed_data_requests: string[];
  runtime_error: string[];
  other: string[];
} {
  const cats = new Set(categories);
  return {
    analysis_failed: cats.has("analysis_failed") ? [_MANIFEST_PLACEHOLDER_LINE] : [],
    failed_data_requests: cats.has("failed_data_requests") ? [_MANIFEST_PLACEHOLDER_LINE] : [],
    runtime_error: cats.has("runtime_error") ? [_MANIFEST_PLACEHOLDER_LINE] : [],
    other: cats.has("other") ? [_MANIFEST_PLACEHOLDER_LINE] : [],
  };
}

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

  /**
   * Phase 5a — fee-reconciliation report, populated by clicking
   * "Reconcile fees" on a loaded run. Reset to null whenever a new
   * response/normalized is set so the panel doesn't carry over a
   * stale report onto a fresh run.
   */
  readonly reconciliation = signal<RunReconciliationReport | null>(null);
  readonly reconciling = signal(false);
  readonly reconcileError = signal<{ reason: string; message: string; status: number } | null>(null);
  /** First 10 divergence rows for at-a-glance review (the full list can be long). */
  readonly reconciliationTopDivergences = computed(() => {
    const r = this.reconciliation();
    if (!r) return [];
    return r.divergences.slice(0, 10);
  });

  /**
   * Phase 5g.3+ — caller-supplied Engine-Lab strategy class name + the
   * Branch-A ``assert_fees`` flag. Per D3 (mission-critical doc), the
   * server does not auto-derive the strategy class from the LEAN-Lab
   * algorithm — the operator must type the exact Python class name
   * (PascalCase, e.g. ``BuyAndHoldStrategy``). Empty string is rejected
   * client-side via the Validators.required + minLength check so a
   * silently-empty submit can't reach the server.
   */
  readonly crossReconcileForm = new FormGroup({
    engineLabStrategyClass: new FormControl("BuyAndHoldStrategy", {
      nonNullable: true,
      validators: [Validators.required, Validators.minLength(1), Validators.maxLength(200)],
    }),
    assertFees: new FormControl(false, { nonNullable: true }),
  });
  readonly crossReconciliation = signal<CrossEngineReconciliationReport | null>(null);
  readonly crossReconciling = signal(false);
  readonly crossReconcileError = signal<{ reason: string; message: string; status: number } | null>(null);
  /** Show only the first 10 divergence rows — full lists can be long. */
  readonly crossReconciliationTopDivergences = computed(() => {
    const r = this.crossReconciliation();
    if (!r) return [];
    return r.divergences.slice(0, 10);
  });
  /**
   * Flatten ``counts_by_category`` into [(category, count)] for the
   * histogram strip. Sorted desc by count then alpha so the top
   * offenders surface first.
   */
  readonly crossReconciliationCategoryCounts = computed(() => {
    const r = this.crossReconciliation();
    if (!r) return [];
    const rows = Object.entries(r.counts_by_category).map(([category, count]) => ({
      category,
      count: count ?? 0,
    }));
    rows.sort((a, b) => b.count - a.count || a.category.localeCompare(b.category));
    return rows;
  });

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
   * P2.5 — returns the int64 ms UTC of 09:30 ET on ``iso``, NOT the
   * midnight-UTC value the pre-P2.5 contract sent. The backend
   * validator now requires 09:30 ET session-open millis; sending
   * midnight UTC gets a 422 naming the offending wall-clock.
   *
   * Conversion goes through Intl.DateTimeFormat with the NY zone so
   * DST is honored (EDT vs EST produce different UTC ms for the same
   * 09:30 wall-clock). A fixed-offset converter would be a silent
   * 1-hour bug on either side of the DST boundaries (2026-03-08 EST→EDT
   * and 2026-11-01 EDT→EST). The DST test surface in
   * lean-lab.component.spec.ts pins both transitions.
   *
   * Parsed strictly to avoid `new Date("YYYY-MM-DD")` browser
   * ambiguity (Chrome parses UTC, Safari parses local — we use
   * explicit numeric parts so neither matters).
   */
  private isoDateToSessionOpenMsUtc(iso: string): number {
    const [y, m, d] = iso.split("-").map((n) => Number.parseInt(n, 10));
    if (!Number.isFinite(y) || !Number.isFinite(m) || !Number.isFinite(d)) {
      throw new Error(`isoDateToSessionOpenMsUtc: invalid ISO date ${iso}`);
    }
    // Resolve the NY UTC offset for the requested date by formatting
    // an arbitrary UTC ms through the NY zone and reading the
    // formatted offset back. Equivalent to tzdata-aware arithmetic
    // without pulling in a date library.
    const fmt = new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      timeZoneName: "longOffset",
    });
    // Pick noon UTC on the requested date — far from any UTC midnight
    // wrap so the formatted parts are unambiguous on every platform.
    const refUtcMs = Date.UTC(y, m - 1, d, 12, 0, 0);
    const parts = fmt.formatToParts(new Date(refUtcMs));
    const offsetPart = parts.find((p) => p.type === "timeZoneName")?.value ?? "";
    // longOffset shapes: "GMT-05:00" (EST) or "GMT-04:00" (EDT).
    const match = /GMT([+-])(\d{2}):(\d{2})/.exec(offsetPart);
    if (!match) {
      throw new Error(
        `isoDateToSessionOpenMsUtc: could not parse NY UTC offset from "${offsetPart}"`,
      );
    }
    const sign = match[1] === "-" ? -1 : 1;
    const offsetHours = Number.parseInt(match[2], 10);
    const offsetMinutes = Number.parseInt(match[3], 10);
    const offsetMs = sign * (offsetHours * 60 + offsetMinutes) * 60 * 1000;
    // 09:30 ET wall - NY offset = UTC. EST (offset -05:00) → 14:30 UTC;
    // EDT (offset -04:00) → 13:30 UTC.
    return Date.UTC(y, m - 1, d, 9, 30, 0) - offsetMs;
  }

  /**
   * Naive next-trading-day computation: add 1 calendar day, then skip
   * Saturday + Sunday. Holidays are NOT skipped — the validator will
   * reject and surface the offending date. The full blocked-aware
   * picker (P2.5 Step 8) calls the /calendar/blocked-dates endpoint
   * to do this properly; until that ships, this approximation
   * handles the common case (Fri → Mon, Mon → Tue) cleanly.
   */
  private nextWeekdayIso(iso: string): string {
    const [y, m, d] = iso.split("-").map((n) => Number.parseInt(n, 10));
    // Construct a UTC Date so getUTCDay/Date arithmetic is consistent
    // across browsers (no local-time DST nonsense).
    let dt = new Date(Date.UTC(y, m - 1, d));
    dt = new Date(dt.getTime() + 86_400_000);
    while (dt.getUTCDay() === 0 || dt.getUTCDay() === 6) {
      dt = new Date(dt.getTime() + 86_400_000);
    }
    return [
      dt.getUTCFullYear(),
      String(dt.getUTCMonth() + 1).padStart(2, "0"),
      String(dt.getUTCDate()).padStart(2, "0"),
    ].join("-");
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
    // Phase 5a: clear any prior reconciliation report so the panel
    // doesn't carry a stale report from a different run.
    this.reconciliation.set(null);
    this.reconcileError.set(null);
    this.crossReconciliation.set(null);
    this.crossReconcileError.set(null);
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
      // Use the actual exit_code + is_clean from the summary row.
      // ``is_clean`` is the true cleanliness signal (exit==0 AND no
      // classified LEAN errors AND not timed out — written from the
      // launcher's response into ``manifest.notes`` as ``is_clean=...``);
      // ``exit_clean`` is just ``exit_code == 0`` and would paint a
      // run with logged LEAN errors as green. Reviewer P1: fall back
      // to ``false`` for legacy manifests where ``is_clean`` is null
      // — under-claim cleanliness, never over-claim it. Same fallback
      // when the summary isn't in the cache (refresh raced the click).
      const exit_code = summary?.exit_code ?? -1;
      const is_clean = summary?.is_clean === true;
      // Phase 4f: populate lean_errors buckets from the manifest's
      // category note. The manifest only stores category NAMES (not
      // line content), so each bucket gets a single placeholder line
      // when the category was hit. This is enough for the existing
      // errorRows()/badge logic to render "LEAN errors logged
      // (failed_data_requests)" instead of the misleading empty-
      // buckets state, while making it explicit in the row text that
      // the line content isn't recoverable from the manifest.
      this.response.set({
        run_id: runId,
        is_clean,
        exit_code,
        duration_ms: 0,
        timed_out: false,
        lean_errors: rehydratedLeanErrors(summary?.lean_error_categories ?? []),
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
    // P2.5: prefer manifest.parameters.{start_date,end_date} (ISO
    // strings derived from the trading-date semantics by the service
    // layer) so the inverse-of-end ambiguity at the FE doesn't matter.
    // Under the new contract end_ms = next_trading_day(end_date)
    // session-open, so naive msUtcToIsoDate would surface the NEXT
    // trading day in the picker — wrong. Fall back to ms for legacy
    // manifests written under schema_version 1.
    const paramStart = manifest.parameters?.["start_date"];
    const paramEnd = manifest.parameters?.["end_date"];
    if (typeof paramStart === "string" && /^\d{4}-\d{2}-\d{2}$/.test(paramStart)) {
      patch.startDate = paramStart;
    }
    if (typeof paramEnd === "string" && /^\d{4}-\d{2}-\d{2}$/.test(paramEnd)) {
      patch.endDate = paramEnd;
    }
    if (!patch.startDate || !patch.endDate) {
      const win = manifest.requested_window_ms;
      if (win && typeof win.start_ms === "number" && typeof win.end_ms === "number") {
        patch.startDate = patch.startDate ?? this.msUtcToIsoDate(win.start_ms);
        patch.endDate = patch.endDate ?? this.msUtcToIsoDate(win.end_ms);
      }
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

  /**
   * Phase 5a — fetch the categorized fee-divergence report for the
   * currently-loaded run. Available only when ``response()`` is set
   * (the template guards on this). Does NOT modify the run; reads the
   * persisted normalized result and runs the IBKR comparison.
   *
   * Error surfacing: the typed envelope from the launcher (404 when
   * the run has no normalized result, 400 for invalid slug) is shown
   * in the panel as a small inline error so the operator can branch
   * without hunting through the global error banner.
   */
  /**
   * Phase 5g.3+ — POST the cross-reconcile request and store the
   * report. Mirrors ``reconcileFees`` for the race-protection pattern:
   * snapshot the run_id at click time and drop the response on the
   * floor if the user navigates away before it returns.
   *
   * Per D10, the response carries ``schema_version`` and the UI MUST
   * fail-fast on an unrecognized version rather than silently
   * misrender — that check happens before ``crossReconciliation()`` is
   * populated.
   */
  async crossReconcile(): Promise<void> {
    const current = this.response();
    if (current === null) return;
    if (this.crossReconcileForm.invalid) {
      this.crossReconcileForm.markAllAsTouched();
      return;
    }
    const requestedRunId = current.run_id;
    const value = this.crossReconcileForm.getRawValue();
    this.crossReconciling.set(true);
    this.crossReconcileError.set(null);
    try {
      const report = await this.service.crossReconcileRun(requestedRunId, {
        engine_lab_strategy_class: value.engineLabStrategyClass.trim(),
        assert_fees: value.assertFees,
      });
      if (this.response()?.run_id !== requestedRunId) {
        // The user navigated away; drop the stale response on the floor.
        return;
      }
      if (report.schema_version !== CROSS_RECONCILE_SCHEMA_VERSION) {
        // D10 fail-fast: this UI build does not know how to render a
        // server response with a different schema version. The user
        // sees a structured error pane with the version mismatch.
        this.crossReconciliation.set(null);
        this.crossReconcileError.set({
          reason: "schema_version_mismatch",
          message: `Server returned schema_version=${report.schema_version}; this UI build understands ${CROSS_RECONCILE_SCHEMA_VERSION}. Rebuild the frontend to match.`,
          status: 200,
        });
        return;
      }
      this.crossReconciliation.set(report);
    } catch (err) {
      if (this.response()?.run_id !== requestedRunId) return;
      this.crossReconciliation.set(null);
      if (err instanceof LeanSidecarApiError) {
        this.crossReconcileError.set({
          reason: err.reason,
          message: err.message,
          status: err.status,
        });
      } else {
        this.crossReconcileError.set({
          reason: "client_error",
          message: err instanceof Error ? err.message : String(err),
          status: 0,
        });
      }
    } finally {
      this.crossReconciling.set(false);
    }
  }

  async reconcileFees(): Promise<void> {
    const current = this.response();
    if (current === null) return;
    // Snapshot the run_id this click was for. If the user navigates to
    // a different run (sidebar click, fresh submit) before the POST
    // returns, we must NOT paint the old run's report onto the new
    // run's panel. Reviewer P2 — race fix.
    const requestedRunId = current.run_id;
    this.reconciling.set(true);
    this.reconcileError.set(null);
    try {
      const report = await this.service.reconcileRun(requestedRunId);
      if (this.response()?.run_id !== requestedRunId) {
        // The user moved on; drop the stale response on the floor.
        return;
      }
      this.reconciliation.set(report);
    } catch (err) {
      if (this.response()?.run_id !== requestedRunId) {
        // Same race for the error path — don't show run A's 404 on
        // run B's panel.
        return;
      }
      this.reconciliation.set(null);
      if (err instanceof LeanSidecarApiError) {
        this.reconcileError.set({
          reason: err.reason,
          message: err.message,
          status: err.status,
        });
      } else {
        this.reconcileError.set({
          reason: "client_error",
          message: err instanceof Error ? err.message : String(err),
          status: 0,
        });
      }
    } finally {
      // Always clear the in-flight indicator. ``reconciling`` is a
      // component-level signal and shouldn't bleed across runs;
      // submit() and the sidebar click already clear ``reconciliation``
      // and ``reconcileError`` when the active run changes, so a brief
      // spinner-cleared-then-button-clickable state on the new run is
      // the correct visual.
      this.reconciling.set(false);
    }
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
    this.reconciliation.set(null);
    this.reconcileError.set(null);
    this.crossReconciliation.set(null);
    this.crossReconcileError.set(null);

    const value = this.form.getRawValue();
    // P2.5 wire contract:
    //   start_ms_utc = 09:30 ET of startDate (first trading day)
    //   end_ms_utc   = 09:30 ET of next_trading_day(endDate) — the
    //                  half-open exclusive end (NOT endDate itself).
    // The full blocked-aware picker (Step 8 of the design hub) is a
    // follow-up; for now we compute next_trading_day as
    // "skip weekends" client-side. If the user picks a Friday before
    // a Monday holiday (e.g., MLK weekend), the validator returns a
    // 422 naming the offending date so the operator can fix the form.
    const start_ms_utc = this.isoDateToSessionOpenMsUtc(value.startDate);
    const exclusiveEndIso = this.nextWeekdayIso(value.endDate);
    const end_ms_utc = this.isoDateToSessionOpenMsUtc(exclusiveEndIso);

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
