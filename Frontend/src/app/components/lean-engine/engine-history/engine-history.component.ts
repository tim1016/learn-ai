import {
  Component, ChangeDetectionStrategy, OnInit,
  computed, inject, output, signal,
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

/**
 * Column registry for the studies table.
 *
 * The history table previously hardcoded sixteen always-visible columns,
 * which was overwhelming for the 90% case where the user only wants to
 * scan recent runs by P&L / Sharpe / DD. ``id`` is the persisted
 * identifier (also used as the localStorage key suffix), ``defaultOn``
 * picks the initial visible set, and ``sortable`` flags which columns
 * are re-orderable via the toggleSort handler.
 */
export interface ColumnDef {
  id: string;
  label: string;
  sortable?: string;     // backend sort key, when sortable
  defaultOn: boolean;
  num?: boolean;
}

export const HISTORY_COLUMNS: readonly ColumnDef[] = [
  { id: 'date',         label: 'Date',     sortable: 'executedat',   defaultOn: true },
  { id: 'strategy',     label: 'Strategy', sortable: 'strategyname', defaultOn: true },
  { id: 'symbol',       label: 'Symbol',                              defaultOn: true },
  { id: 'range',        label: 'Range',                               defaultOn: true },
  { id: 'totalPnl',     label: 'Net P&L',  sortable: 'totalpnl',     defaultOn: true,  num: true },
  { id: 'sharpe',       label: 'Sharpe',   sortable: 'sharpe',       defaultOn: true,  num: true },
  { id: 'maxDrawdown',  label: 'Max DD',   sortable: 'drawdown',     defaultOn: true,  num: true },
  { id: 'winRate',      label: 'Win %',    sortable: 'winrate',      defaultOn: true,  num: true },
  { id: 'totalTrades',  label: 'Trades',   sortable: 'trades',       defaultOn: true,  num: true },
  { id: 'cagr',         label: 'CAGR',     sortable: 'cagr',         defaultOn: false, num: true },
  { id: 'sortino',      label: 'Sortino',  sortable: 'sortino',      defaultOn: false, num: true },
  { id: 'psr',          label: 'PSR',      sortable: 'psr',          defaultOn: false, num: true },
  { id: 'profitFactor', label: 'PF',       sortable: 'profitfactor', defaultOn: false, num: true },
  { id: 'var95',        label: 'VaR 95',   sortable: 'var95',        defaultOn: false, num: true },
  { id: 'params',       label: 'Params',                              defaultOn: false },
  { id: 'notes',        label: 'Notes',                               defaultOn: false },
];

const COLUMN_PREF_KEY = 'engine-history.columns.v1';

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

  /** Emits when user clicks the Replay button on a row. */
  replayRequested = output<StudyListItem>();

  /** ID of the row currently being edited for notes. */
  editingNotesId = signal<number | null>(null);
  editingNotesValue = signal('');

  /** All known columns (immutable registry). */
  readonly allColumns = HISTORY_COLUMNS;

  /** User-selected visible-column ids. Persisted to localStorage so the
   *  picker survives reloads. Defaults to columns flagged ``defaultOn``. */
  readonly visibleIds = signal<Set<string>>(this.loadColumnPrefs());

  /** Visible column defs in registry order. */
  readonly visibleColumns = computed(() =>
    this.allColumns.filter(c => this.visibleIds().has(c.id))
  );

  /** Open/closed state for the column-chooser dropdown. */
  readonly chooserOpen = signal(false);

  toggleChooser(): void {
    this.chooserOpen.update(v => !v);
  }

  toggleColumn(id: string): void {
    this.visibleIds.update(set => {
      const next = new Set(set);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      this.persistColumnPrefs(next);
      return next;
    });
  }

  resetColumns(): void {
    const defaults = new Set(this.allColumns.filter(c => c.defaultOn).map(c => c.id));
    this.visibleIds.set(defaults);
    this.persistColumnPrefs(defaults);
  }

  private loadColumnPrefs(): Set<string> {
    try {
      const raw = localStorage.getItem(COLUMN_PREF_KEY);
      if (raw) {
        const parsed = JSON.parse(raw) as string[];
        if (Array.isArray(parsed) && parsed.length > 0) {
          // Filter to ids that still exist in the registry.
          return new Set(parsed.filter(id => this.allColumns.some(c => c.id === id)));
        }
      }
    } catch {
      // Corrupt prefs — fall through to defaults.
    }
    return new Set(this.allColumns.filter(c => c.defaultOn).map(c => c.id));
  }

  private persistColumnPrefs(set: Set<string>): void {
    try {
      localStorage.setItem(COLUMN_PREF_KEY, JSON.stringify([...set]));
    } catch {
      // Quota exceeded / private mode — non-fatal.
    }
  }

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

  onReplayClick(study: StudyListItem, event: MouseEvent): void {
    event.stopPropagation();
    this.replayRequested.emit(study);
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
