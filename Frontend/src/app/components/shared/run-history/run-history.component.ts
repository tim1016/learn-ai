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
  readonly allowCompare = input<boolean>(true);

  readonly compareRequested = output<{ leftId: string; rightId: string }>();
  readonly runSelected = output<string>();
  /** PR B.3 (2026-05-19) — emitted when the user saves a notes edit on a row.
   *  The host component owns the persistence side (GraphQL mutation). */
  readonly notesEdited = output<{ id: string; notes: string }>();

  // Ordered array — preserves the sequence in which the user checked rows.
  private readonly _selectedIds = signal<readonly string[]>([]);
  private readonly _editingId = signal<string | null>(null);
  private readonly _editingValue = signal<string>("");

  readonly selectedIds = computed(() => this._selectedIds());
  readonly canCompare = computed(
    () => this.allowCompare() && this._selectedIds().length === 2,
  );
  readonly isEmpty = computed(() => this.rows().length === 0);
  readonly editingId = computed(() => this._editingId());
  readonly editingValue = computed(() => this._editingValue());

  badge(source: EngineSourceLiteral): string {
    return ENGINE_LABELS[source];
  }

  /**
   * PR B.3 — compact summary of the persisted DataPolicy bars pair, formatted
   * as ``m/1 → m/15`` (input → strategy). Falls back to a single token when
   * the two specs are equal (typical for daily-resolution runs). Returns an
   * em-dash for legacy rows without a DataPolicy block so the column doesn't
   * collapse visually.
   */
  barsSummary(dp: DataPolicy | null): string {
    if (!dp) return "—";
    const code = (timespan: string): string => {
      switch (timespan) {
        case "minute": return "m";
        case "hour": return "h";
        case "day": return "d";
        default: return timespan;
      }
    };
    const i = `${code(dp.input_bars.timespan)}/${dp.input_bars.multiplier}`;
    const s = `${code(dp.strategy_bars.timespan)}/${dp.strategy_bars.multiplier}`;
    return i === s ? i : `${i} → ${s}`;
  }

  isSelected(id: string): boolean {
    return this._selectedIds().includes(id);
  }

  toggle(id: string): void {
    this._selectedIds.update((ids) =>
      ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id],
    );
  }

  emitCompare(): void {
    const ids = this._selectedIds();
    if (ids.length !== 2) return;
    this.compareRequested.emit({ leftId: ids[0], rightId: ids[1] });
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
