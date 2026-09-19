import { ChangeDetectionStrategy, Component, computed, input, model } from '@angular/core';

import {
  exportTimeZoneOptions,
  toggleExportColumn,
  type ExportColumnSelection,
} from '../export-csv-options';

/**
 * dataset.csv column and time choices (owner decision 2026-09-19).
 *
 * Presentation only: the selectable columns and the readable time column's
 * header come from the Python plan receipt; the parent owns where the
 * choices are kept. `unix_ts` is always exported and cannot be unticked.
 */
@Component({
  selector: 'app-export-column-picker',
  templateUrl: './export-column-picker.component.html',
  styleUrl: './export-column-picker.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ExportColumnPickerComponent {
  /** The plan receipt's `output_columns`, in canonical order. */
  readonly columns = input.required<readonly string[]>();
  /** The plan receipt's `time_column` — the header the chosen zone produces. */
  readonly timeColumn = input<string | null>(null);
  readonly selection = model.required<ExportColumnSelection>();
  readonly timeZone = model.required<string | null>();

  readonly zoneOptions = computed(() => exportTimeZoneOptions(this.timeZone()));

  private readonly included = computed(() => new Set(this.selection() ?? this.columns()));

  readonly includedCount = computed(
    () => this.columns().filter(c => this.included().has(c)).length,
  );

  isIncluded(column: string): boolean {
    return this.included().has(column);
  }

  onToggle(column: string, event: Event): void {
    const checked = (event.target as HTMLInputElement).checked;
    this.selection.set(toggleExportColumn(this.selection(), this.columns(), column, checked));
  }

  /** Back to "everything", which also includes columns added later. */
  selectAll(): void {
    this.selection.set(null);
  }

  selectNone(): void {
    this.selection.set([]);
  }

  onTimeZoneChange(event: Event): void {
    const value = (event.target as HTMLSelectElement).value;
    this.timeZone.set(value === '' ? null : value);
  }
}
