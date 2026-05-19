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

const ENGINE_LABELS: Record<EngineSourceLiteral, string> = {
  engine: "Engine Lab",
  "strategy-lab": "Strategy Lab",
  "lean-sidecar": "LEAN",
};

@Component({
  selector: "app-run-history",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CurrencyPipe],
  templateUrl: "./run-history.component.html",
  styleUrl: "./run-history.component.scss",
})
export class RunHistoryComponent {
  readonly rows = input.required<RunHistoryRow[]>();
  readonly allowCompare = input<boolean>(true);

  readonly compareRequested = output<{ leftId: string; rightId: string }>();
  readonly runSelected = output<string>();

  // Ordered array — preserves the sequence in which the user checked rows.
  private readonly _selectedIds = signal<readonly string[]>([]);

  readonly selectedIds = computed(() => this._selectedIds());
  readonly canCompare = computed(
    () => this.allowCompare() && this._selectedIds().length === 2,
  );
  readonly isEmpty = computed(() => this.rows().length === 0);

  badge(source: EngineSourceLiteral): string {
    return ENGINE_LABELS[source];
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
}
