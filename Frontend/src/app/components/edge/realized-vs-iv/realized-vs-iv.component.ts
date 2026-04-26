import { ChangeDetectionStrategy, Component, computed, inject, signal } from "@angular/core";
import { RouterLink } from "@angular/router";
import {
  EdgePriceIVChartComponent,
  EdgeVrpHistogramComponent,
  EDGE_TOK,
} from "../charts/edge-charts";
import { EdgeScoreDrawerComponent } from "../drawers/edge-score-drawer.component";
import { TradeSimDrawerComponent } from "../drawers/trade-sim-drawer.component";
import { EdgeApiService, type Estimator } from "../services/edge-api.service";
import { EdgeMockDataService, type EdgeData } from "../services/edge-mock-data.service";

@Component({
  selector: "app-edge-realized-vs-iv",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    EdgePriceIVChartComponent,
    EdgeVrpHistogramComponent,
    EdgeScoreDrawerComponent,
    TradeSimDrawerComponent,
  ],
  templateUrl: "./realized-vs-iv.component.html",
  styleUrls: ["./realized-vs-iv.component.scss"],
})
export class RealizedVsIvComponent {
  private readonly mockData = inject(EdgeMockDataService);
  private readonly api = inject(EdgeApiService);
  protected readonly TOK = EDGE_TOK;

  /** Mutable signal — defaults to mock data, swapped to live data on Compute. */
  readonly data = signal<EdgeData>(this.mockData.get());
  readonly loading = signal(false);
  readonly errorMsg = signal<string | null>(null);
  readonly liveSource = signal(false);

  readonly symbol = signal("SPY");
  readonly barSize = signal<"5m" | "15m" | "1h" | "1D">("1D");
  readonly tenor = signal<"7D" | "14D" | "30D" | "60D">("30D");

  readonly estimators = signal<Record<Estimator, boolean>>({
    ctc: false, parkinson: false, gk: false, yz: true,
  });

  readonly hoverIdx = signal<number | null>(null);
  readonly currentIdx = computed(() => this.hoverIdx() ?? Math.floor(this.data().N / 2));

  readonly showOracle = signal(true);
  readonly showRealtime = signal(true);
  readonly rvBands = signal(true);
  readonly edgeStrip = signal(true);

  readonly layers = computed(() => ({
    rvBands: this.rvBands(),
    edgeStrip: this.edgeStrip(),
  }));

  readonly scoreOpen = signal(false);
  readonly tradeSimOpen = signal(false);

  readonly blindStartDate = computed(() => {
    const d = this.data();
    const i = Math.max(0, d.coverage.bars_total - d.coverage.forward_blind_tail);
    return d.dates[i]?.toISOString().slice(0, 10) ?? "—";
  });

  readonly readout = computed(() => {
    const d = this.data();
    const i = this.currentIdx();
    const z = d.vrpZ[i];
    const action = Number.isNaN(z) ? "—"
      : z > 1 ? "SHORT VOL"
      : z < -1 ? "LONG VOL"
      : "NO TRADE";
    const color = action === "SHORT VOL" ? this.TOK.bear
      : action === "LONG VOL" ? this.TOK.bull : this.TOK.muted;
    return {
      i, z, action, color,
      iv30: d.iv30[i],
      rvYZ: d.rvYZ[i],
      rvForward: d.rvForward[i],
      vrpForward: d.vrpForward[i],
      edgeScore: d.edgeScore[i],
    };
  });

  readonly readoutHistogramN = computed(() =>
    this.data().vrpHistogram.reduce((acc, b) => acc + b.count, 0),
  );

  readonly currentScoreLabel = computed(() => {
    const s = this.data().edgeScore[this.currentIdx()] ?? 0;
    const sign = s >= 0 ? "+" : "";
    return `${sign}${s.toFixed(2)}`;
  });
  readonly currentScoreColor = computed(() =>
    (this.data().edgeScore[this.currentIdx()] ?? 0) >= 0 ? this.TOK.bull : this.TOK.bear
  );

  protected async onCompute(): Promise<void> {
    this.loading.set(true);
    this.errorMsg.set(null);
    try {
      const enabled = (Object.entries(this.estimators()) as [Estimator, boolean][])
        .filter(([, on]) => on)
        .map(([k]) => k);
      const partial = await this.api.computeRealizedVsIv({
        symbol: this.symbol(),
        barSize: this.barSize(),
        tenor: this.tenor(),
        estimators: enabled.length ? enabled : ["yz"],
      });
      // Merge over mock data so chart components keep all expected fields.
      this.data.set({ ...this.mockData.get(), ...partial } as EdgeData);
      this.liveSource.set(true);
      this.hoverIdx.set(null);
    } catch (e) {
      this.errorMsg.set((e as Error).message);
    } finally {
      this.loading.set(false);
    }
  }

  protected toggleEstimator(k: Estimator): void {
    this.estimators.update((m) => ({ ...m, [k]: !m[k] }));
  }
  protected estimatorOn(k: Estimator): boolean { return this.estimators()[k]; }

  protected fmtPct(v: number, digits = 1): string {
    if (Number.isNaN(v)) return "—";
    return (v * 100).toFixed(digits) + "%";
  }
  protected fmtSigned(v: number, digits = 2): string {
    if (Number.isNaN(v)) return "—";
    const sign = v >= 0 ? "+" : "";
    return sign + v.toFixed(digits);
  }
}
