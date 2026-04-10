import {
  Component, inject, signal, output, OnInit,
  ChangeDetectionStrategy,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../../../environments/environment';

export interface StudyListItem {
  id: number;
  symbol: string;
  strategyName: string;
  startDate: string;
  endDate: string;
  timespan: string;
  fillMode: string;
  source: string;
  totalTrades: number;
  winningTrades: number;
  losingTrades: number;
  winRate: number;
  totalPnL: number;
  maxDrawdown: number;
  sharpeRatio: number;
  sortinoRatio: number;
  compoundingAnnualReturn: number;
  probabilisticSharpeRatio: number;
  profitFactor: number;
  valueAtRisk95: number;
  alpha: number;
  beta: number;
  initialCash: number;
  finalEquity: number;
  parameters: string;
  notes: string | null;
  executedAt: string;
  durationMs: number;
}

interface StudyListResponse {
  items: StudyListItem[];
  totalCount: number;
  page: number;
  pageSize: number;
}

interface SortState {
  column: string;
  direction: 'asc' | 'desc';
}

@Component({
  selector: 'app-engine-history',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './engine-history.component.html',
  styleUrls: ['./engine-history.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class EngineHistoryComponent implements OnInit {
  private http = inject(HttpClient);
  private readonly apiBase = (environment.backendUrl ?? 'http://localhost:5000').replace(/\/graphql$/, '');

  studies = signal<StudyListItem[]>([]);
  loading = signal(false);
  error = signal<string | null>(null);
  totalCount = signal(0);
  page = signal(1);
  sort = signal<SortState>({ column: 'executedat', direction: 'desc' });

  /** Emits when user clicks a row to load a past study. */
  studySelected = output<number>();

  /** ID of the row currently being edited for notes. */
  editingNotesId = signal<number | null>(null);
  editingNotesValue = signal('');

  ngOnInit(): void {
    this.loadStudies();
  }

  async loadStudies(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const s = this.sort();
      const url = `${this.apiBase}/api/studies`;
      const params: Record<string, string> = {
        page: String(this.page()),
        pageSize: '50',
        sortBy: s.column,
        sortDir: s.direction,
      };
      const resp = await firstValueFrom(
        this.http.get<StudyListResponse>(url, { params })
      );
      this.studies.set(resp.items);
      this.totalCount.set(resp.totalCount);
    } catch (err: any) {
      this.error.set(err?.message ?? 'Failed to load studies');
    } finally {
      this.loading.set(false);
    }
  }

  toggleSort(column: string): void {
    const current = this.sort();
    if (current.column === column) {
      this.sort.set({ column, direction: current.direction === 'asc' ? 'desc' : 'asc' });
    } else {
      this.sort.set({ column, direction: 'desc' });
    }
    this.page.set(1);
    this.loadStudies();
  }

  sortIcon(column: string): string {
    const s = this.sort();
    if (s.column !== column) return 'pi-sort-alt';
    return s.direction === 'asc' ? 'pi-sort-amount-up' : 'pi-sort-amount-down';
  }

  onRowClick(study: StudyListItem): void {
    this.studySelected.emit(study.id);
  }

  startEditNotes(study: StudyListItem, event: MouseEvent): void {
    event.stopPropagation();
    this.editingNotesId.set(study.id);
    this.editingNotesValue.set(study.notes ?? '');
  }

  async saveNotes(study: StudyListItem): Promise<void> {
    const url = `${this.apiBase}/api/studies/${study.id}/notes`;
    try {
      await firstValueFrom(
        this.http.patch(url, { notes: this.editingNotesValue() })
      );
      study.notes = this.editingNotesValue();
    } catch {
      // silent — note not saved
    }
    this.editingNotesId.set(null);
  }

  cancelEditNotes(): void {
    this.editingNotesId.set(null);
  }

  formatPct(val: number): string {
    return (val * 100).toFixed(2) + '%';
  }

  formatCurrency(val: number): string {
    return new Intl.NumberFormat('en-US', {
      style: 'currency', currency: 'USD', maximumFractionDigits: 0,
    }).format(val);
  }

  formatDate(iso: string): string {
    if (!iso) return '';
    return new Date(iso).toLocaleDateString('en-US', {
      month: 'short', day: 'numeric', year: '2-digit',
      hour: '2-digit', minute: '2-digit',
    });
  }

  parseParams(json: string): string {
    try {
      const obj = JSON.parse(json);
      return Object.entries(obj)
        .map(([k, v]) => `${k}=${v}`)
        .join(', ');
    } catch {
      return json;
    }
  }

  pnlClass(val: number): string {
    return val > 0 ? 'positive' : val < 0 ? 'negative' : '';
  }
}
