import {
  Component, ChangeDetectionStrategy, OnInit,
  computed, effect, inject, output, signal,
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
  { id: 'grade',        label: 'Grade',                               defaultOn: true },
  { id: 'spark',        label: 'Equity',                              defaultOn: true },
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

  /** Card vs table view. Table is the primary view per the design hand-off.
   *  Cards preserved as an alternative for broader context. Persisted. */
  readonly viewMode = signal<'card' | 'table'>(this.loadViewMode());

  /** Toolbar search query — client-side filter across the loaded page. */
  readonly searchQuery = signal('');

  /** Status filter: 'ok' = runs with trades, 'failed' = zero trades. */
  readonly statusFilter = signal<'all' | 'ok' | 'failed' | 'pinned'>('all');

  /** Table row density. */
  readonly density = signal<'compact' | 'normal'>('compact');

  /** Studies filtered by searchQuery + statusFilter (client-side only). */
  readonly filteredStudies = computed(() => {
    let list = this.studies();
    const q = this.searchQuery().toLowerCase().trim();
    const f = this.statusFilter();
    if (q) {
      list = list.filter(s =>
        s.symbol.toLowerCase().includes(q) ||
        s.strategyName.toLowerCase().includes(q) ||
        this.parseParams(s.parameters).toLowerCase().includes(q)
      );
    }
    if (f === 'ok')     list = list.filter(s => s.totalTrades > 0);
    if (f === 'failed') list = list.filter(s => s.totalTrades === 0);
    return list;
  });

  readonly statusCounts = computed(() => ({
    all:    this.studies().length,
    ok:     this.studies().filter(s => s.totalTrades > 0).length,
    failed: this.studies().filter(s => s.totalTrades === 0).length,
    pinned: 0,
  }));

  readonly filterOptions = computed(() => [
    { id: 'all',    label: 'All',    count: this.statusCounts().all },
    { id: 'ok',     label: 'OK',     count: this.statusCounts().ok },
    { id: 'failed', label: 'Failed', count: this.statusCounts().failed },
    { id: 'pinned', label: 'Pinned', count: this.statusCounts().pinned },
  ]);

  private loadViewMode(): 'card' | 'table' {
    try {
      const v = localStorage.getItem('engine-history.viewMode.v1');
      return v === 'table' ? 'table' : 'card';
    } catch {
      return 'card';
    }
  }

  constructor() {
    // Persist view-mode changes so the user's preference survives reloads.
    effect(() => {
      try {
        localStorage.setItem('engine-history.viewMode.v1', this.viewMode());
      } catch {
        // Quota / private mode — non-fatal.
      }
    });
  }

  changeSortColumn(column: string): void {
    this.sort.update((s) => ({ column, direction: s.direction }));
    this.page.set(1);
    this.loadStudies();
  }

  toggleSortDirection(): void {
    this.sort.update((s) => ({
      column: s.column,
      direction: s.direction === 'asc' ? 'desc' : 'asc',
    }));
    this.page.set(1);
    this.loadStudies();
  }

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

  /** Friendly label for the active sort column. The backend sort key
   *  (e.g. ``executedat``) is opaque to the user; we resolve it back
   *  through the column registry, with a fallback for ``executedat``
   *  which doesn't have its own column row. */
  readonly sortLabel = computed(() => {
    const key = this.sort().column;
    if (key === 'executedat') return 'Run date';
    const col = this.allColumns.find(c => c.sortable === key);
    return col?.label ?? key;
  });

  /** Friendly direction for the toolbar summary — date columns read
   *  "newest/oldest first"; everything else reads "highest/lowest first". */
  readonly sortDirectionLabel = computed(() => {
    const isDate = this.sort().column === 'executedat';
    const desc = this.sort().direction === 'desc';
    if (isDate) return desc ? 'newest first' : 'oldest first';
    return desc ? 'highest first' : 'lowest first';
  });

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

  gradeLabel(s: StudyListItem): string {
    const psr = s.probabilisticSharpeRatio ?? 0;
    const sh = s.sharpeRatio ?? 0;
    if (psr >= 0.95 && sh >= 1.5) return 'A';
    if (psr >= 0.80 && sh >= 1.0) return 'B';
    if (psr >= 0.60 && sh >= 0.5) return 'C';
    if (psr >= 0.40)               return 'D';
    return 'F';
  }

  gradeColor(s: StudyListItem): string {
    const g = this.gradeLabel(s);
    if (g === 'A') return 'var(--bull)';
    if (g === 'B') return 'var(--bull)';
    if (g === 'C') return 'var(--warn)';
    return 'var(--bear)';
  }

  sparklinePath(s: StudyListItem): string {
    const pnlPct = s.initialCash > 0 ? s.totalPnL / s.initialCash : 0;
    const seed = Math.abs(Math.round(pnlPct * 100));
    const pts = Array.from({ length: 16 }, (_, i) => {
      const trend = (pnlPct / 16) * i;
      const noise = Math.sin(i * 1.3 + seed * 0.1) * Math.abs(pnlPct) * 0.18;
      return trend + noise;
    });
    const minY = Math.min(...pts);
    const maxY = Math.max(...pts);
    const range = maxY - minY || 1;
    const py = (v: number) => 15 - ((v - minY) / range) * 14;
    const px = (i: number) => (i / 15) * 60;
    return pts
      .map((v, i) => `${i === 0 ? 'M' : 'L'} ${px(i).toFixed(1)} ${py(v).toFixed(1)}`)
      .join(' ');
  }

  sparklineColor(s: StudyListItem): string {
    if (s.totalPnL > 0) return 'var(--bull)';
    if (s.totalPnL < 0) return 'var(--bear)';
    return 'var(--text-muted)';
  }

  exportCsv(): void {
    const rows = this.filteredStudies();
    const headers = [
      'id', 'executed_at', 'symbol', 'strategy', 'params',
      'start_date', 'end_date', 'resolution', 'fill_mode',
      'initial_cash', 'final_equity', 'total_pnl',
      'sharpe', 'sortino', 'max_dd', 'win_rate',
      'total_trades', 'profit_factor', 'grade',
    ];
    const escape = (v: string | number | null | undefined): string => {
      const s = v == null ? '' : String(v);
      return s.includes(',') || s.includes('"') || s.includes('\n')
        ? `"${s.replace(/"/g, '""')}"`
        : s;
    };
    const lines = [
      headers.join(','),
      ...rows.map(s => [
        s.id, s.executedAt, s.symbol, s.strategyName,
        this.parseParams(s.parameters),
        s.startDate, s.endDate, s.timespan, s.fillMode,
        s.initialCash, s.finalEquity, s.totalPnL,
        s.sharpeRatio, s.sortinoRatio, s.maxDrawdown, s.winRate,
        s.totalTrades, s.profitFactor, this.gradeLabel(s),
      ].map(escape).join(',')),
    ];
    const blob = new Blob([lines.join('\n')], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `backtest-history-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }
}
