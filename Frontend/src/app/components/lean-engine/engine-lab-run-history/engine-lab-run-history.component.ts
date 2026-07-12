import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  output,
  signal,
} from "@angular/core";
import { Router } from "@angular/router";
import { Apollo } from "apollo-angular";
import { toSignal } from "@angular/core/rxjs-interop";
import { firstValueFrom } from "rxjs";
import { map } from "rxjs/operators";

import { RunHistoryComponent } from "../../shared/run-history/run-history.component";
import {
  BACKTEST_RUNS_QUERY,
  BacktestRunNode,
  BacktestRunsQueryResult,
  Engine,
  toRunHistoryRow,
  UPDATE_BACKTEST_RUN_NOTES_MUTATION,
} from "../../../graphql/backtest-runs.query";
import { JobsService } from "../../../services/jobs.service";

/** Persisted filter selection. ``ALL`` keeps the GraphQL variable null. */
type EngineFilter = "ALL" | Engine;

const COLUMN_PREF_KEY = "engine-lab-history.columns.v1";

/** Job types whose successful completion should refresh the History table.
 *  ``engine_backtest`` is the Python engine path today. ``lean_engine_run``
 *  is the planned LEAN sidecar path (issue #470) — listing it here now
 *  means the auto-refresh starts working for LEAN runs the moment that
 *  ships, with no change to this component. */
const ENGINE_JOB_TYPES = new Set<string>(["engine_backtest", "lean_engine_run"]);

/**
 * PR B.3 (2026-05-19) — unified history surface. Hosts the Engine filter
 * dropdown + CSV export + column visibility chooser around the shared
 * <see cref="RunHistoryComponent"/>, and persists inline notes edits via the
 * new <c>updateBacktestRunNotes</c> mutation. The legacy REST-backed
 * <c>EngineHistoryComponent</c> retires in Task 3.6; the features that
 * actually got used (notes / CSV / column toggle) are ported forward here.
 */
@Component({
  selector: "app-engine-lab-run-history",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RunHistoryComponent],
  templateUrl: "./engine-lab-run-history.component.html",
  styleUrl: "./engine-lab-run-history.component.scss",
})
export class EngineLabRunHistoryComponent {
  private readonly apollo = inject(Apollo);
  private readonly router = inject(Router);
  private readonly jobsService = inject(JobsService);
  /** Job ids we've already refetched for. Without this, every signal tick
   *  while a completed engine job sits in `JobsService.jobs` would refire
   *  the refetch — the user would see a refetch storm on subsequent filter
   *  changes or recentLogs ticks. */
  private readonly seenCompletedIds = new Set<string>();

  /** Active engine filter — drives the GraphQL ``engine`` variable. */
  readonly engineFilter = signal<EngineFilter>("ALL");

  /** Emitted when a row is clicked — the parent component routes this to
   *  the Results tab (replacing the deleted REST EngineHistoryComponent's
   *  studySelected output). The id is the StrategyExecution numeric id as
   *  a string (GraphQL ID). */
  readonly runSelected = output<string>();

  /** Column-visibility set, persisted to localStorage so a researcher's
   *  layout survives reloads. Defaults to "core columns only" so the
   *  first-time experience isn't overwhelming. */
  readonly visibleColumns = signal<Set<ColumnId>>(this.loadColumnPrefs());

  /** Drives the column-chooser dropdown open state. */
  readonly chooserOpen = signal(false);

  private readonly queryRef = this.apollo.watchQuery<BacktestRunsQueryResult>({
    query: BACKTEST_RUNS_QUERY,
    variables: { engine: null, first: 50 },
    fetchPolicy: "cache-and-network",
  });

  readonly rows = toSignal(
    this.queryRef.valueChanges.pipe(
      map((r) => {
        const nodes = r.data?.backtestRuns?.nodes;
        if (!nodes) return [];
        return (nodes as BacktestRunNode[]).map(toRunHistoryRow);
      }),
    ),
    { initialValue: [] },
  );

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
    // Re-fetch whenever the engine filter changes. The dropdown is a 3-state
    // (ALL | PYTHON | LEAN) selector and ALL maps to a null GraphQL variable.
    effect(() => {
      const filter = this.engineFilter();
      void this.queryRef.refetch({
        engine: filter === "ALL" ? null : filter,
        first: 50,
      });
    });

    // Re-fetch whenever an engine-type job transitions to ``completed``.
    // The runs persist before the SSE ``job.completed`` event fires (see
    // ``lean_sidecar_service.run_trusted_sample`` and the Python engine
    // job worker), so the row is already in the DB by the time we refetch.
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
      // ``refetch()`` with no args reuses the last-set variables — the
      // current engine filter / pagination is preserved.
      void this.queryRef.refetch();
    });
  }

  setEngineFilter(value: string): void {
    if (value === "ALL" || value === "PYTHON" || value === "LEAN") {
      this.engineFilter.set(value as EngineFilter);
    }
  }

  onCompare(event: { leftId: string; rightId: string }): void {
    void this.router.navigate(["/runs/compare"], {
      queryParams: { left: event.leftId, right: event.rightId },
    });
  }

  onRowSelected(id: string): void {
    this.runSelected.emit(id);
    void this.router.navigate(["/engine/runs", id]);
  }

  // ------------------------------------------------------------------
  // Notes — round-trip through the new updateBacktestRunNotes mutation.
  // Apollo's normalized cache picks up the mutation result (id + notes
  // selection) and updates the watched query without a refetch.
  // ------------------------------------------------------------------
  async onNotesEdited(event: { id: string; notes: string }): Promise<void> {
    try {
      await firstValueFrom(
        this.apollo.mutate({
          mutation: UPDATE_BACKTEST_RUN_NOTES_MUTATION,
          variables: { id: Number(event.id), notes: event.notes },
        }),
      );
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
  // CSV export — client-side serialization of the GraphQL result.
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
      if (visible.has("window")) cells.push(r.startDate, r.endDate);
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
