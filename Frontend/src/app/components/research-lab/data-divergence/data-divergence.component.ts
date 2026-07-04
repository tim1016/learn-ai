import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  signal, OnInit,
} from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { DomSanitizer, SafeResourceUrl } from '@angular/platform-browser';
import { firstValueFrom } from 'rxjs';

import { environment } from '../../../../environments/environment';

type Timeframe = '5m' | '15m' | '1h';

interface RebuildResponse {
  path: string;
  size_mb: number;
  timeframe: Timeframe;
}

interface MatrixRow {
  indicator: string;
  impl: string;
  n: number;
  median_abs: number | null;
  p95_abs: number | null;
  max_abs: number | null;
  corr: number | null;
}

interface MatrixResponse {
  timeframe: Timeframe;
  rows: MatrixRow[];
}

@Component({
  selector: 'app-data-divergence',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './data-divergence.component.html',
  styleUrls: ['./data-divergence.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class DataDivergenceComponent implements OnInit {
  private readonly http = inject(HttpClient);
  private readonly sanitizer = inject(DomSanitizer);

  // Available timeframes; only 15m has data today, others appear when the
  // research module is run for them.
  readonly timeframes: readonly Timeframe[] = ['5m', '15m', '1h'];

  // Currently selected timeframe.
  readonly selectedTimeframe = signal<Timeframe>('15m');

  // UI state
  readonly isRebuilding = signal(false);
  readonly rebuildError = signal<string | null>(null);
  readonly rebuildInfo = signal<RebuildResponse | null>(null);
  readonly matrixSummary = signal<MatrixRow[] | null>(null);
  readonly cacheBuster = signal(Date.now());

  // Computed: the iframe src URL. Cache-buster query param ensures a
  // post-rebuild reload pulls the fresh HTML instead of a browser cache hit.
  readonly dashboardUrl = computed<SafeResourceUrl>(() => {
    const tf = this.selectedTimeframe();
    const buster = this.cacheBuster();
    const url = `${environment.pythonServiceUrl}/research/data-divergence/dashboard.html?tf=${tf}&_=${buster}`;
    return this.sanitizer.bypassSecurityTrustResourceUrl(url);
  });

  // Computed: a short summary of headline matrix stats for the inline panel.
  readonly headlineRows = computed(() => {
    const rows = this.matrixSummary();
    if (!rows) return null;
    const interesting = ['ema_20', 'ema_200', 'rsi_14', 'macd_line', 'adx_14'];
    return rows
      .filter(r => interesting.includes(r.indicator))
      .sort((a, b) => a.indicator.localeCompare(b.indicator));
  });

  // Open dashboard in a separate browser tab — useful for full-screen review.
  openInNewTab(): void {
    const tf = this.selectedTimeframe();
    const url = `${environment.pythonServiceUrl}/research/data-divergence/dashboard.html?tf=${tf}`;
    window.open(url, '_blank');
  }

  // POST /research/data-divergence/rebuild — regenerates the static HTML
  // from current cache contents on the backend.
  async rebuild(): Promise<void> {
    if (this.isRebuilding()) return;
    this.isRebuilding.set(true);
    this.rebuildError.set(null);
    try {
      const tf = this.selectedTimeframe();
      const url = `${environment.pythonServiceUrl}/research/data-divergence/rebuild?tf=${tf}`;
      const response = await firstValueFrom(this.http.post<RebuildResponse>(url, {}));
      this.rebuildInfo.set(response);
      // Force the iframe to reload with the freshly-built HTML
      this.cacheBuster.set(Date.now());
      // Refresh the inline matrix panel too
      await this.fetchMatrix();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      this.rebuildError.set(`Rebuild failed: ${msg}`);
    } finally {
      this.isRebuilding.set(false);
    }
  }

  // GET /research/data-divergence/matrix/{tf} — pulls the raw JSON for the
  // inline summary panel below the iframe.
  async fetchMatrix(): Promise<void> {
    try {
      const tf = this.selectedTimeframe();
      const url = `${environment.pythonServiceUrl}/research/data-divergence/matrix/${tf}`;
      const response = await firstValueFrom(this.http.get<MatrixResponse>(url));
      this.matrixSummary.set(response.rows);
    } catch (err) {
      // Non-fatal — the matrix may not be built yet for this timeframe.
      this.matrixSummary.set(null);
    }
  }

  selectTimeframe(tf: Timeframe): void {
    this.selectedTimeframe.set(tf);
    this.cacheBuster.set(Date.now());
    this.fetchMatrix();
  }

  ngOnInit(): void {
    this.fetchMatrix();
  }
}
