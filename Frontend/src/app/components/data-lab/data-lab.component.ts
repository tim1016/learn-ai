import {
  Component, signal, computed, inject, viewChild,
  ChangeDetectionStrategy,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { CommonModule } from '@angular/common';
import { RouterModule } from '@angular/router';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../../environments/environment';
import { DataLabChartComponent, ChartIndicatorEntry } from './data-lab-chart/data-lab-chart.component';
import { DataLabSessionService, DataLabSessionSummary, DataLabSessionChartSnapshot } from '../../services/data-lab-session.service';

interface ParamConfig {
  name: string;
  type: string;
  default: number;
  min: number;
  max: number;
  description: string;
}

interface IndicatorInfo {
  name: string;
  category: string;
  description: string;
  configurable_params: ParamConfig[];
}

interface IndicatorEntry {
  name: string;
  params: Record<string, number>;
}

interface AvailableResponse {
  success: boolean;
  categories: Record<string, IndicatorInfo[]>;
  total: number;
}

interface CategoryData {
  name: string;
  indicators: IndicatorInfo[];
}

// Default indicators pre-selected on load
const DEFAULT_ENTRIES: IndicatorEntry[] = [
  { name: 'ema', params: { length: 5 } },
  { name: 'ema', params: { length: 10 } },
  { name: 'ema', params: { length: 20 } },
  { name: 'ema', params: { length: 30 } },
  { name: 'ema', params: { length: 40 } },
  { name: 'ema', params: { length: 50 } },
  { name: 'ema', params: { length: 100 } },
  { name: 'ema', params: { length: 200 } },
  { name: 'bbands', params: { length: 20, std: 2.0 } },
  { name: 'supertrend', params: { length: 10, multiplier: 3.0 } },
  { name: 'macd', params: { fast: 12, slow: 26, signal: 9 } },
];

@Component({
  selector: 'app-data-lab',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterModule, DataLabChartComponent],
  templateUrl: './data-lab.component.html',
  styleUrls: ['./data-lab.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class DataLabComponent {
  private http = inject(HttpClient);
  private sessionService = inject(DataLabSessionService);

  /** Reference to the chart child so we can call loadCachedData(). */
  chartComponent = viewChild<DataLabChartComponent>('chartComponent');

  // ── Session management state ──────────────────────────────
  savedSessions = signal<DataLabSessionSummary[]>([]);
  activeSessionId = signal<string | null>(null);
  sessionPanelOpen = signal(false);
  savingSession = signal(false);
  sessionName = signal('');
  renamingSessionId = signal<string | null>(null);
  renameValue = signal('');

  /** The latest chart snapshot received from the chart component. */
  private latestChartSnapshot = signal<DataLabSessionChartSnapshot | null>(null);

  ticker = signal('SPY');
  fromDate = signal('2025-03-28');
  toDate = signal('2026-03-28');
  session = signal<'rth' | 'extended'>('rth');
  forwardFill = signal(true);

  loading = signal(false);
  loadingIndicators = signal(false);
  loadingValidation = signal(false);
  error = signal('');
  progress = signal('');

  // Validation report state
  validationReport = signal('');
  ourCsvFile = signal<File | null>(null);
  tvCsvFile = signal<File | null>(null);

  categories = signal<CategoryData[]>([]);
  indicatorMap = signal<Record<string, IndicatorInfo>>({});

  // Active indicator entries (each with name + params)
  entries = signal<IndicatorEntry[]>([...DEFAULT_ENTRIES]);

  // Which indicator we're configuring (for the add form)
  addingIndicator = signal<string>('');
  addParams = signal<Record<string, number>>({});

  expandedCategories = signal<Set<string>>(new Set());

  // Selected indicator names (unique set for checkbox state)
  get selectedNames(): Set<string> {
    return new Set(this.entries().map(e => e.name));
  }

  entryCount = computed(() => this.entries().length);

  // Chart-compatible indicator entries (same shape, typed for chart component)
  chartIndicators = computed<ChartIndicatorEntry[]>(() =>
    this.entries().map(e => ({ name: e.name, params: e.params }))
  );

  // Estimated output columns
  estimatedColumns = computed(() => {
    const base = ['unix_ts', 'iso_time', 'open', 'high', 'low', 'close', 'volume', 'vwap', 'transactions'];
    const indicatorCols: string[] = [];
    for (const entry of this.entries()) {
      const info = this.indicatorMap()[entry.name];
      // Estimate columns based on known multi-column indicators
      const paramStr = Object.entries(entry.params).map(([k, v]) => `${v}`).join('_');
      const suffix = paramStr ? `_${paramStr}` : '';
      const multiCol: Record<string, string[]> = {
        bbands: [`bbl${suffix}`, `bbm${suffix}`, `bbu${suffix}`, `bbb${suffix}`, `bbp${suffix}`],
        macd: [`macd${suffix}`, `macdh${suffix}`, `macds${suffix}`],
        supertrend: [`supert${suffix}`, `supertd${suffix}`, `supertl${suffix}`, `superts${suffix}`],
        stoch: [`stochk${suffix}`, `stochd${suffix}`],
        aroon: [`aroond${suffix}`, `aroonu${suffix}`, `aroonosc${suffix}`],
        kc: [`kcl${suffix}`, `kcb${suffix}`, `kcu${suffix}`],
        donchian: [`dcl${suffix}`, `dcm${suffix}`, `dcu${suffix}`],
        adx: [`adx${suffix}`, `dmp${suffix}`, `dmn${suffix}`],
      };
      if (multiCol[entry.name]) {
        indicatorCols.push(...multiCol[entry.name]);
      } else {
        indicatorCols.push(`${entry.name}${suffix}`);
      }
    }
    return [...base, ...indicatorCols];
  });

  constructor() {
    this.loadAvailableIndicators();
    this.refreshSessionList();
  }

  // ── Session management ─────────────────────────────────────

  async refreshSessionList(): Promise<void> {
    this.savedSessions.set(await this.sessionService.listSessions());
  }

  toggleSessionPanel(): void {
    this.sessionPanelOpen.update(v => !v);
    if (this.sessionPanelOpen()) this.refreshSessionList();
  }

  /**
   * Called by the chart component's (dataLoaded) event.
   * Captures the chart payload so the next "Save" includes it.
   */
  onChartDataLoaded(event: {
    bars: any[];
    indicators: any[];
    quality: any;
    allowedTimeframes: string[];
    estimatedBarsPerTimeframe: Record<string, number>;
    recommendedTimeframe: string;
    visibleIndicatorIds: string[];
    timeframe: string;
  }): void {
    this.latestChartSnapshot.set({
      timeframe: event.timeframe,
      bars: event.bars,
      indicators: event.indicators,
      quality: event.quality,
      allowedTimeframes: event.allowedTimeframes,
      estimatedBarsPerTimeframe: event.estimatedBarsPerTimeframe,
      recommendedTimeframe: event.recommendedTimeframe,
      visibleIndicatorIds: event.visibleIndicatorIds,
    });

    // Auto-update snapshot if this is an active (already-saved) session
    const activeId = this.activeSessionId();
    if (activeId) {
      this.sessionService.updateChartSnapshot(activeId, this.latestChartSnapshot()!);
    }
  }

  async saveSession(): Promise<void> {
    this.savingSession.set(true);
    try {
      const config = {
        ticker: this.ticker(),
        fromDate: this.fromDate(),
        toDate: this.toDate(),
        session: this.session(),
        forwardFill: this.forwardFill(),
        entries: this.entries(),
      };

      const activeId = this.activeSessionId();
      if (activeId) {
        // Update existing session
        await this.sessionService.updateSession(
          activeId,
          config,
          this.latestChartSnapshot(),
          this.sessionName() || config.ticker
        );
      } else {
        // Create new session
        const newId = await this.sessionService.saveSession(
          config,
          this.latestChartSnapshot(),
          this.sessionName() || undefined
        );
        if (newId) this.activeSessionId.set(newId);
      }

      await this.refreshSessionList();
    } finally {
      this.savingSession.set(false);
    }
  }

  async saveAsNewSession(): Promise<void> {
    this.activeSessionId.set(null); // Force new
    await this.saveSession();
  }

  async loadSession(id: string): Promise<void> {
    const session = await this.sessionService.getSession(id);
    if (!session) return;

    // Restore configuration
    this.ticker.set(session.config.ticker);
    this.fromDate.set(session.config.fromDate);
    this.toDate.set(session.config.toDate);
    this.session.set(session.config.session);
    this.forwardFill.set(session.config.forwardFill);
    this.entries.set([...session.config.entries]);
    this.activeSessionId.set(session.id);
    this.sessionName.set(session.name);

    // Restore chart data from snapshot (no API call)
    if (session.chartSnapshot) {
      this.latestChartSnapshot.set(session.chartSnapshot);

      // Give Angular a tick to propagate input changes to the chart child
      setTimeout(() => {
        const chart = this.chartComponent();
        if (chart) {
          chart.loadCachedData({
            bars: session.chartSnapshot!.bars,
            indicators: session.chartSnapshot!.indicators,
            quality: session.chartSnapshot!.quality,
            allowedTimeframes: session.chartSnapshot!.allowedTimeframes,
            estimatedBarsPerTimeframe: session.chartSnapshot!.estimatedBarsPerTimeframe,
            recommendedTimeframe: session.chartSnapshot!.recommendedTimeframe,
            visibleIndicatorIds: session.chartSnapshot!.visibleIndicatorIds,
            timeframe: session.chartSnapshot!.timeframe,
          });
        }
      });
    }

    this.sessionPanelOpen.set(false);
  }

  async deleteSession(id: string, event: Event): Promise<void> {
    event.stopPropagation();
    await this.sessionService.deleteSession(id);
    if (this.activeSessionId() === id) {
      this.activeSessionId.set(null);
      this.sessionName.set('');
    }
    await this.refreshSessionList();
  }

  startRenaming(session: DataLabSessionSummary, event: Event): void {
    event.stopPropagation();
    this.renamingSessionId.set(session.id);
    this.renameValue.set(session.name);
  }

  async confirmRename(id: string): Promise<void> {
    if (this.renameValue().trim()) {
      await this.sessionService.renameSession(id, this.renameValue().trim());
      await this.refreshSessionList();
    }
    this.renamingSessionId.set(null);
  }

  cancelRename(): void {
    this.renamingSessionId.set(null);
  }

  formatDate(timestamp: string): string {
    return new Date(timestamp).toLocaleDateString('en-US', {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  }

  detachSession(): void {
    this.activeSessionId.set(null);
    this.sessionName.set('');
  }

  async loadAvailableIndicators(): Promise<void> {
    this.loadingIndicators.set(true);
    try {
      const response = await firstValueFrom(
        this.http.get<AvailableResponse>(
          `${environment.pythonServiceUrl}/api/dataset/available`
        )
      );

      if (!response.success) {
        this.error.set('Failed to load indicators');
        return;
      }

      const catList: CategoryData[] = [];
      const map: Record<string, IndicatorInfo> = {};
      for (const [catName, items] of Object.entries(response.categories)) {
        catList.push({ name: catName, indicators: items });
        for (const item of items) {
          map[item.name] = item;
        }
      }
      this.categories.set(catList);
      this.indicatorMap.set(map);
    } catch (e: unknown) {
      this.error.set(e instanceof Error ? e.message : String(e));
    } finally {
      this.loadingIndicators.set(false);
    }
  }

  toggleCategory(catName: string): void {
    this.expandedCategories.update(set => {
      const next = new Set(set);
      next.has(catName) ? next.delete(catName) : next.add(catName);
      return next;
    });
  }

  isCategoryExpanded(catName: string): boolean {
    return this.expandedCategories().has(catName);
  }

  isSelected(name: string): boolean {
    return this.entries().some(e => e.name === name);
  }

  categorySelectedCount(catName: string): number {
    const cat = this.categories().find(c => c.name === catName);
    if (!cat) return 0;
    const names = this.selectedNames;
    return cat.indicators.filter(i => names.has(i.name)).length;
  }

  toggleIndicator(ind: IndicatorInfo): void {
    if (this.isSelected(ind.name)) {
      // Remove all entries for this indicator
      this.entries.update(list => list.filter(e => e.name !== ind.name));
    } else {
      // Add with defaults
      const defaults: Record<string, number> = {};
      for (const p of ind.configurable_params) {
        defaults[p.name] = p.default;
      }
      this.entries.update(list => [...list, { name: ind.name, params: defaults }]);
    }
  }

  addInstance(ind: IndicatorInfo): void {
    const defaults: Record<string, number> = {};
    for (const p of ind.configurable_params) {
      defaults[p.name] = p.default;
    }
    this.entries.update(list => [...list, { name: ind.name, params: defaults }]);
  }

  removeEntry(index: number): void {
    this.entries.update(list => list.filter((_, i) => i !== index));
  }

  updateEntryParam(index: number, paramName: string, value: number): void {
    this.entries.update(list => {
      const next = [...list];
      next[index] = { ...next[index], params: { ...next[index].params, [paramName]: value } };
      return next;
    });
  }

  clearAll(): void {
    this.entries.set([]);
  }

  resetDefaults(): void {
    this.entries.set([...DEFAULT_ENTRIES]);
  }

  entryLabel(entry: IndicatorEntry): string {
    const parts = Object.entries(entry.params).map(([, v]) => v);
    return parts.length ? `${entry.name}(${parts.join(', ')})` : entry.name;
  }

  getConfigParams(name: string): ParamConfig[] {
    return this.indicatorMap()[name]?.configurable_params ?? [];
  }

  async generateCsv(): Promise<void> {
    this.loading.set(true);
    this.error.set('');
    this.progress.set('Fetching OHLCV data and calculating indicators...');

    try {
      const payload = {
        ticker: this.ticker(),
        from_date: this.fromDate(),
        to_date: this.toDate(),
        indicator_entries: this.entries(),
        session: this.session(),
        forward_fill: this.forwardFill(),
      };

      const blob = await firstValueFrom(
        this.http.post(
          `${environment.pythonServiceUrl}/api/dataset/generate-csv`,
          payload,
          { responseType: 'blob' }
        )
      );

      const sessionLabel = this.session() === 'rth' ? 'rth' : 'ext';
      this.progress.set('Downloading CSV...');
      this.downloadBlob(blob, `${this.ticker()}_minute_${sessionLabel}_${this.fromDate()}_to_${this.toDate()}.csv`);
      this.progress.set('Done! CSV downloaded.');
    } catch (e: unknown) {
      this.error.set(e instanceof Error ? e.message : String(e));
      this.progress.set('');
    } finally {
      this.loading.set(false);
    }
  }

  async downloadMetadata(): Promise<void> {
    this.error.set('');
    try {
      const payload = {
        ticker: this.ticker(),
        from_date: this.fromDate(),
        to_date: this.toDate(),
        indicator_entries: this.entries(),
        session: this.session(),
        forward_fill: this.forwardFill(),
      };

      const blob = await firstValueFrom(
        this.http.post(
          `${environment.pythonServiceUrl}/api/dataset/generate-metadata`,
          payload,
          { responseType: 'blob' }
        )
      );

      const sessionLabel = this.session() === 'rth' ? 'rth' : 'ext';
      this.downloadBlob(blob, `${this.ticker()}_minute_${sessionLabel}_${this.fromDate()}_to_${this.toDate()}_metadata.json`);
    } catch (e: unknown) {
      this.error.set(e instanceof Error ? e.message : String(e));
    }
  }

  async downloadColumnsCsv(): Promise<void> {
    this.error.set('');
    try {
      const payload = {
        ticker: this.ticker(),
        from_date: this.fromDate(),
        to_date: this.toDate(),
        indicator_entries: this.entries(),
        session: this.session(),
        forward_fill: this.forwardFill(),
      };

      const blob = await firstValueFrom(
        this.http.post(
          `${environment.pythonServiceUrl}/api/dataset/generate-metadata-csv`,
          payload,
          { responseType: 'blob' }
        )
      );

      const sessionLabel = this.session() === 'rth' ? 'rth' : 'ext';
      this.downloadBlob(blob, `${this.ticker()}_minute_${sessionLabel}_${this.fromDate()}_to_${this.toDate()}_columns.csv`);
    } catch (e: unknown) {
      this.error.set(e instanceof Error ? e.message : String(e));
    }
  }

  onOurCsvSelected(event: Event): void {
    const input = event.target as HTMLInputElement;
    this.ourCsvFile.set(input.files?.[0] ?? null);
  }

  onTvCsvSelected(event: Event): void {
    const input = event.target as HTMLInputElement;
    this.tvCsvFile.set(input.files?.[0] ?? null);
  }

  async runValidation(): Promise<void> {
    const ourFile = this.ourCsvFile();
    const tvFile = this.tvCsvFile();
    if (!ourFile || !tvFile) return;

    this.loadingValidation.set(true);
    this.error.set('');
    this.validationReport.set('');

    try {
      const formData = new FormData();
      formData.append('our_csv', ourFile);
      formData.append('tv_csv', tvFile);
      formData.append('ticker', this.ticker());

      const response = await firstValueFrom(
        this.http.post<{ success: boolean; report: string }>(
          `${environment.pythonServiceUrl}/api/dataset/validation-report`,
          formData
        )
      );

      if (response.success) {
        this.validationReport.set(response.report);
      } else {
        this.error.set('Validation report generation failed');
      }
    } catch (e: unknown) {
      this.error.set(e instanceof Error ? e.message : String(e));
    } finally {
      this.loadingValidation.set(false);
    }
  }

  downloadValidationReport(): void {
    const report = this.validationReport();
    if (!report) return;
    const blob = new Blob([report], { type: 'text/markdown' });
    this.downloadBlob(blob, `${this.ticker()}_validation_report.md`);
  }

  private downloadBlob(blob: Blob, filename: string): void {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }
}
