import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
  signal,
} from "@angular/core";
import { CurrencyPipe } from "@angular/common";
import { EngineSourceLiteral, RunHistoryRow } from "./run-history.types";
import type { DataPolicy } from "../../../models/data-policy";
import { TimestampDisplayPipe } from "../../../shared/timestamp";

const ENGINE_LABELS: Record<EngineSourceLiteral, string> = {
  engine: "Engine Lab",
  "strategy-lab": "Strategy Lab",
  "lean-sidecar": "LEAN",
};

@Component({
  selector: "app-run-history",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CurrencyPipe, TimestampDisplayPipe],
  templateUrl: "./run-history.component.html",
  styleUrl: "./run-history.component.scss",
})
export class RunHistoryComponent {
  readonly rows = input.required<RunHistoryRow[]>();
  readonly runSelected = output<string>();
  /** PR B.3 (2026-05-19) — emitted when the user saves a notes edit on a row.
   *  The host component owns the persistence side (GraphQL mutation). */
  readonly notesEdited = output<{ id: string; notes: string }>();

  private readonly _editingId = signal<string | null>(null);
  private readonly _editingValue = signal<string>("");

  readonly isEmpty = computed(() => this.rows().length === 0);
  readonly editingId = computed(() => this._editingId());
  readonly editingValue = computed(() => this._editingValue());

  badge(source: EngineSourceLiteral): string {
    return ENGINE_LABELS[source];
  }

  strategyLabel(row: RunHistoryRow): string {
    if (row.source === "lean-sidecar" && row.strategyName === "user_provided") {
      return "User-modified algorithm";
    }
    return row.strategyName;
  }

  /**
   * PR B.3 — explicit summary of the persisted DataPolicy bars pair. Keep
   * input bars and strategy bars named so LEAN runs that consume M1 data but
   * calculate indicators on M15 consolidated bars never look like an M1
   * strategy.
   */
  barsSummary(dp: DataPolicy | null): string {
    if (!dp) return "—";
    const code = (timespan: string): string => {
      switch (timespan) {
        case "minute": return "M";
        case "hour": return "H";
        case "day": return "D";
        default: return timespan;
      }
    };
    const label = (bars: DataPolicy['input_bars']): string =>
      `${code(bars.timespan)}${bars.multiplier}`;
    const input = label(dp.input_bars);
    const strategy = label(dp.strategy_bars);
    return input === strategy ? `Input and strategy ${strategy}` : `Input ${input} / Strategy ${strategy}`;
  }

  onRowClick(id: string): void {
    this.runSelected.emit(id);
  }

  // ------------------------------------------------------------------
  // PR B.3 — inline notes editing. The shared component owns the UI
  // affordance (button → input → save/cancel) while persistence stays
  // with the host via the ``notesEdited`` output. Keeps the GraphQL
  // dependency out of this shared widget.
  // ------------------------------------------------------------------
  startEditNotes(row: RunHistoryRow, event: MouseEvent): void {
    event.stopPropagation();
    this._editingId.set(row.id);
    this._editingValue.set(row.notes ?? "");
  }

  updateEditingValue(value: string): void {
    this._editingValue.set(value);
  }

  saveNotes(row: RunHistoryRow): void {
    this.notesEdited.emit({ id: row.id, notes: this._editingValue() });
    this._editingId.set(null);
  }

  cancelEditNotes(): void {
    this._editingId.set(null);
  }
}
