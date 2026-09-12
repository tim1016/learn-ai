import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  output,
  resource,
  signal,
} from "@angular/core";
import { firstValueFrom } from "rxjs";

import { RunHistoryComponent } from "../../shared/run-history/run-history.component";
import { BacktestRunsService, HISTORY_PAGE_SIZE } from "../../../services/backtest-runs.service";
import { toRunHistoryRow, type BacktestRunSummary, type Engine, runWindowDate } from "../../../services/backtest-runs.types";
import { JobsService } from "../../../services/jobs.service";
import { GoldenValidationService } from "../../../services/golden-validation.service";

/** Persisted filter selection. ``ALL`` asks for every engine. */
type EngineFilter = "ALL" | Engine;

const COLUMN_PREF_KEY = "engine-lab-history.columns.v1";

/** Job types whose successful completion should refresh the History table:
 *  ``engine_backtest`` is the Python engine path, ``lean_engine_run`` the
 *  LEAN sidecar path. */
const ENGINE_JOB_TYPES = new Set<string>(["engine_backtest", "lean_engine_run"]);

/**
 * Unified history surface. Hosts the Engine filter dropdown + CSV export +
 * column visibility chooser around the shared <see cref="RunHistoryComponent"/>,
 * and persists inline notes edits through the run service (PRD #1929).
 *
 * The list is a `resource`: the engine filter is a params change, while a
 * job completing is a `reload()` — a params change discards the previous
 * value and would blank the table mid-refresh.
 */
@Component({
  selector: "app-engine-lab-run-history",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RunHistoryComponent],
  templateUrl: "./engine-lab-run-history.component.html",
  styleUrl: "./engine-lab-run-history.component.scss",
})
export class EngineLabRunHistoryComponent {
  private readonly runs = inject(BacktestRunsService);
  private readonly jobsService = inject(JobsService);
  private readonly goldenValidation = inject(GoldenValidationService);
  /** Job ids we've already refreshed for. Without this, every signal tick
   *  while a completed engine job sits in `JobsService.jobs` would refire
   *  the reload — the user would see a refresh storm on subsequent filter
   *  changes or recentLogs ticks. */
  private readonly seenCompletedIds = new Set<string>();

  /** Active engine filter — drives the list's ``engine`` parameter. */
  readonly engineFilter = signal<EngineFilter>("ALL");

  /** Emitted when a row is clicked — the parent component routes this to
   *  the Results tab. The id is the run's numeric id as a string. */
  readonly runSelected = output<string>();
  readonly goldenRunRequested = output<number>();
  /** Monotonic host-owned refresh token after a Golden designation or review. */
  readonly goldenRefreshToken = input(0);

  /** Column-visibility set, persisted to localStorage so a researcher's
   *  layout survives reloads. Defaults to "core columns only" so the
   *  first-time experience isn't overwhelming. */
  readonly visibleColumns = signal<Set<ColumnId>>(this.loadColumnPrefs());

  /** Drives the column-chooser dropdown open state. */
  readonly chooserOpen = signal(false);

  private readonly history = resource<BacktestRunSummary[], { engine: Engine | null }>({
    params: () => ({ engine: this.engineFilter() === "ALL" ? null : (this.engineFilter() as Engine) }),
    loader: ({ params }) => firstValueFrom(this.runs.list(params.engine, HISTORY_PAGE_SIZE)),
  });

  private readonly goldenCatalog = resource({
    loader: () => firstValueFrom(this.goldenValidation.list()),
  });

  readonly rows = computed(() => {
    const goldenByRunId = new Map(
      (this.goldenCatalog.hasValue() ? this.goldenCatalog.value() : []).map((entry) => [entry.source_run_id, entry.state]),
    );
    return (this.history.hasValue() ? this.history.value() : []).map((run) => ({
      ...toRunHistoryRow(run),
      goldenValidationState: goldenByRunId.get(run.id) ?? null,
    }));
  });

  /** All known toggleable columns. Order is the rendering order. */
  readonly allColumns: readonly ColumnDef[] = [
    { id: "engine", label: "Engine", defaultOn: true },
    { id: "strategy", label: "Strategy", defaultOn: true },
    { id: "symbol", label: "Symbol", defaultOn: true },
    { id: "window", label: "Window", defaultOn: true },
    { id: "bars", label: "Bars", defaultOn: true },
    { id: "trades", label: "Trades", defaultOn: true },
    { id: "pnl", label: "Net PnL", defaultOn: true },
    { id: "notes", label: "Notes", defaultOn: true },
  ];

  constructor() {
    // Reload whenever an engine-type job transitions to ``completed``. The
    // runs persist before the SSE ``job.completed`` event fires (see
    // ``lean_sidecar_service.run_trusted_sample`` and the Python engine
    // job worker), so the row is already in the table by the time we reload.
    // The seen-ids set is mutated as a non-signal side effect so the
    // effect doesn't re-trigger on its own writes.
    effect(() => {
      const allJobs = this.jobsService.jobs();
      const newlyCompleted = allJobs.filter(
        (j) =>
          ENGINE_JOB_TYPES.has(j.type) &&
          j.status === "completed" &&
          !this.seenCompletedIds.has(j.id),
      );
      if (newlyCompleted.length === 0) return;
      newlyCompleted.forEach((j) => this.seenCompletedIds.add(j.id));
      this.history.reload();
    });
    effect(() => {
      if (this.goldenRefreshToken() > 0) this.goldenCatalog.reload();
    });
  }

  setEngineFilter(value: string): void {
    if (value === "ALL" || value === "PYTHON" || value === "LEAN") {
      this.engineFilter.set(value as EngineFilter);
    }
  }

  onRowSelected(id: string): void {
    this.runSelected.emit(id);
  }

  onGoldenRequested(id: string): void {
    const runId = Number(id);
    if (Number.isInteger(runId) && runId > 0) this.goldenRunRequested.emit(runId);
  }

  // ------------------------------------------------------------------
  // Notes — persisted through the run service; the loaded page is
  // patched in place with the value the server kept, so no reload.
  // ------------------------------------------------------------------
  async onNotesEdited(event: { id: string; notes: string }): Promise<void> {
    try {
      const saved = await firstValueFrom(this.runs.updateNotes(Number(event.id), event.notes));
      this.history.update((runs) => runs?.map((run) => (run.id === saved.id ? { ...run, notes: saved.notes } : run)));
    } catch (err) {
      console.warn("notes update failed", { id: event.id, error: err });
    }
  }

  // ------------------------------------------------------------------
  // Column visibility
  // ------------------------------------------------------------------
  isColumnVisible(id: ColumnId): boolean {
    return this.visibleColumns().has(id);
  }

  toggleColumn(id: ColumnId): void {
    this.visibleColumns.update((set) => {
      const next = new Set(set);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      this.persistColumnPrefs(next);
      return next;
    });
  }

  resetColumns(): void {
    const defaults = new Set(this.allColumns.filter((c) => c.defaultOn).map((c) => c.id));
    this.visibleColumns.set(defaults);
    this.persistColumnPrefs(defaults);
  }

  toggleChooser(): void {
    this.chooserOpen.update((v) => !v);
  }

  private loadColumnPrefs(): Set<ColumnId> {
    try {
      const raw = localStorage.getItem(COLUMN_PREF_KEY);
      if (raw) {
        const parsed = JSON.parse(raw) as string[];
        if (Array.isArray(parsed) && parsed.length > 0) {
          return new Set(parsed.filter(isColumnId));
        }
      }
    } catch {
      // Corrupt prefs — fall through to defaults.
    }
    // Default = everything except notes (matches the first-time-user
    // expectation of "show me the essentials").
    return new Set<ColumnId>(["engine", "strategy", "symbol", "window", "bars", "trades", "pnl", "notes"]);
  }

  private persistColumnPrefs(set: Set<ColumnId>): void {
    try {
      localStorage.setItem(COLUMN_PREF_KEY, JSON.stringify([...set]));
    } catch {
      // Quota exceeded / private mode — non-fatal.
    }
  }

  // ------------------------------------------------------------------
  // CSV export — client-side serialization of the loaded page.
  // Preserves the column-visibility selection so the file matches
  // what the user sees on screen.
  // ------------------------------------------------------------------
  readonly canExport = computed(() => this.rows().length > 0);

  exportCsv(): void {
    const rows = this.rows();
    if (rows.length === 0) return;

    const visible = this.visibleColumns();
    const headers: string[] = [];
    if (visible.has("engine")) headers.push("engine", "source");
    if (visible.has("strategy")) headers.push("strategy");
    if (visible.has("symbol")) headers.push("symbol");
    if (visible.has("window")) headers.push("start_date", "end_date");
    if (visible.has("bars")) headers.push("input_bars", "strategy_indicator_bars");
    if (visible.has("trades")) headers.push("total_trades");
    if (visible.has("pnl")) headers.push("total_pnl");
    if (visible.has("notes")) headers.push("notes");

    const escape = (v: string | number | null | undefined): string => {
      const s = v == null ? "" : String(v);
      return s.includes(",") || s.includes('"') || s.includes("\n")
        ? `"${s.replace(/"/g, '""')}"`
        : s;
    };

    const csvRows = rows.map((r) => {
      const cells: (string | number | null)[] = [];
      if (visible.has("engine")) cells.push(r.engine, r.source);
      if (visible.has("strategy")) cells.push(r.strategyName);
      if (visible.has("symbol")) cells.push(r.symbol);
      if (visible.has("window")) cells.push(runWindowDate(r.startDate), runWindowDate(r.endDate));
      if (visible.has("bars")) {
        const dp = r.dataPolicy;
        cells.push(
          dp ? `${dp.input_bars.timespan}/${dp.input_bars.multiplier}` : "",
          dp ? `${dp.strategy_bars.timespan}/${dp.strategy_bars.multiplier}` : "",
        );
      }
      if (visible.has("trades")) cells.push(r.totalTrades);
      if (visible.has("pnl")) cells.push(r.totalPnl);
      if (visible.has("notes")) cells.push(r.notes);
      return cells.map(escape).join(",");
    });

    const csv = [headers.join(","), ...csvRows].join("\n");
    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `engine-lab-history-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }
}

export type ColumnId =
  | "engine"
  | "strategy"
  | "symbol"
  | "window"
  | "bars"
  | "trades"
  | "pnl"
  | "notes";

interface ColumnDef {
  id: ColumnId;
  label: string;
  defaultOn: boolean;
}

function isColumnId(id: string): id is ColumnId {
  return ["engine", "strategy", "symbol", "window", "bars", "trades", "pnl", "notes"].includes(id);
}
