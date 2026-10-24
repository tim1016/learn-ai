import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';
import { ButtonModule } from 'primeng/button';
import { InputText } from 'primeng/inputtext';

import { ReceiptLabelPipe } from '../pipes/receipt-label.pipe';

export interface ResearchHistoryFilters {
  symbol?: string;
  status?: string;
}

/** Symbol / status filters plus Refresh, shared by the research histories, each of which is one strategy's; emits the whole filter set. */
@Component({
  selector: 'app-research-history-filters',
  imports: [ButtonModule, InputText, ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="filters">
      <label><span>Symbol</span><input #symbolFilter pInputText type="text" placeholder="Any" (input)="set('symbol', symbolFilter.value)" /></label>
      <label><span>Status</span>
        <select (change)="onEvent('status', $event)" aria-label="Filter by status">
          <option value="">All</option>
          @for (status of statuses(); track status) { <option [value]="status">{{ status | receiptLabel }}</option> }
        </select>
      </label>
      <button pButton type="button" size="small" severity="secondary" [outlined]="true" (click)="refresh.emit()"><i class="pi pi-refresh" aria-hidden="true"></i><span>Refresh</span></button>
    </div>
  `,
  styles: `
    :host { display: block; }
    .filters { display: flex; flex-wrap: wrap; gap: var(--space-2); align-items: end; }
    .filters label { display: grid; gap: var(--space-1); font-size: var(--fs-xs); }
  `,
})
export class ResearchHistoryFiltersComponent {
  readonly statuses = input.required<readonly string[]>();
  readonly filterChange = output<ResearchHistoryFilters>();
  readonly refresh = output();

  private current: ResearchHistoryFilters = {};

  onEvent(key: keyof ResearchHistoryFilters, event: Event): void {
    const target = event.target;
    if (target instanceof HTMLSelectElement || target instanceof HTMLInputElement) this.set(key, target.value);
  }

  set(key: keyof ResearchHistoryFilters, raw: string): void {
    const kept = Object.fromEntries(Object.entries(this.current).filter(([name]) => name !== key)) as ResearchHistoryFilters;
    this.current = raw ? { ...kept, [key]: raw } : kept;
    this.filterChange.emit(this.current);
  }
}
