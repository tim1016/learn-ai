import { ChangeDetectionStrategy, Component, computed, inject, signal } from "@angular/core";
import {
  EDGE_TOK,
  EdgeRegimePriceChartComponent,
  EdgeRegimeRadarComponent,
  EdgeStabilitySparklineComponent,
  EdgeTransitionMatrixComponent,
} from "../charts/edge-charts";
import { EdgeScoreDrawerComponent } from "../drawers/edge-score-drawer.component";
import { TradeSimDrawerComponent } from "../drawers/trade-sim-drawer.component";
import { EdgeMockDataService } from "../services/edge-mock-data.service";

type AlgoMode = "hmm" | "kmeans" | "compare";
type ViewMode = "viterbi" | "posterior";

@Component({
  selector: "app-edge-regimes",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    EdgeRegimePriceChartComponent,
    EdgeTransitionMatrixComponent,
    EdgeRegimeRadarComponent,
    EdgeStabilitySparklineComponent,
    EdgeScoreDrawerComponent,
    TradeSimDrawerComponent,
  ],
  templateUrl: "./regimes.component.html",
  styleUrls: ["./regimes.component.scss"],
})
export class RegimesComponent {
  private readonly mockData = inject(EdgeMockDataService);
  protected readonly TOK = EDGE_TOK;

  readonly data = this.mockData.get();
  readonly hoverIdx = signal<number | null>(null);
  readonly viewMode = signal<ViewMode>("viterbi");
  readonly algo = signal<AlgoMode>("hmm");
  readonly scoreOpen = signal(false);
  readonly tradeSimOpen = signal(false);

  readonly regimeNames: readonly string[] = [
    "Trending · low vol",
    "Choppy · high vol",
    "Trending · high vol",
  ];
  readonly regimeColors: readonly string[] = [
    this.TOK.reg1, this.TOK.reg2, this.TOK.reg3,
  ];

  readonly radarAxes: readonly string[] = ["trend", "rv_yz", "atr%", "vol_z", "iv30", "skew", "term"];
  readonly radarData: readonly (readonly number[])[] = [
    [0.85, 0.30, 0.25, 0.20, 0.45, 0.50, 0.55],
    [0.20, 0.80, 0.85, 0.90, 0.85, 0.30, 0.40],
    [0.75, 0.65, 0.60, 0.50, 0.70, 0.55, 0.50],
  ];

  readonly currentScoreLabel = computed(() => {
    const s = this.data.edgeScore[Math.floor(this.data.N / 2)];
    return (s >= 0 ? "+" : "") + s.toFixed(2);
  });
  readonly currentScoreColor = computed(() =>
    this.data.edgeScore[Math.floor(this.data.N / 2)] >= 0 ? this.TOK.bull : this.TOK.bear
  );

  readonly priceChartHeight = computed(() => this.algo() === "compare" ? 320 : 260);

  protected setAlgo(a: AlgoMode): void { this.algo.set(a); }
  protected setView(v: ViewMode): void { this.viewMode.set(v); }

  protected sharpeColor(s: number): string {
    return s >= 0 ? this.TOK.bull : this.TOK.bear;
  }
  protected sharpeBarLeft(pnl: number): string {
    return pnl < 0 ? `${50 - Math.abs(pnl) / 4}%` : "50%";
  }
  protected sharpeBarWidth(pnl: number): string {
    return `${Math.min(50, Math.abs(pnl) / 4)}%`;
  }
  protected fmtSharpe(v: number): string {
    return (v >= 0 ? "+" : "") + v.toFixed(2);
  }
}
