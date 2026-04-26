import { ChangeDetectionStrategy, Component, computed, inject, signal } from "@angular/core";
import { RouterLink } from "@angular/router";
import {
  EDGE_TOK,
  EdgeMiniLineComponent,
  EdgeSharpeHeatmapComponent,
  type HeatmapHover,
} from "../charts/edge-charts";
import { EdgeScoreDrawerComponent } from "../drawers/edge-score-drawer.component";
import { TradeSimDrawerComponent } from "../drawers/trade-sim-drawer.component";
import { EdgeMockDataService } from "../services/edge-mock-data.service";

type SplitMode = "rolling" | "calendar" | "walk-forward";
type Composite = "per-asset" | "equal-weight" | "vol-weighted";

@Component({
  selector: "app-edge-cross-asset",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    EdgeMiniLineComponent,
    EdgeSharpeHeatmapComponent,
    EdgeScoreDrawerComponent,
    TradeSimDrawerComponent,
  ],
  templateUrl: "./cross-asset.component.html",
  styleUrls: ["./cross-asset.component.scss"],
})
export class CrossAssetComponent {
  private readonly mockData = inject(EdgeMockDataService);
  protected readonly TOK = EDGE_TOK;

  readonly data = this.mockData.get();

  readonly splitMode = signal<SplitMode>("rolling");
  readonly composite = signal<Composite>("per-asset");
  readonly hoverCell = signal<HeatmapHover | null>(null);
  readonly scoreOpen = signal(false);
  readonly tradeSimOpen = signal(false);

  readonly splitModes: readonly SplitMode[] = ["rolling", "calendar", "walk-forward"];
  readonly composites: readonly { k: Composite; l: string }[] = [
    { k: "per-asset", l: "Per-asset (small mult.)" },
    { k: "equal-weight", l: "Equal-weight composite" },
    { k: "vol-weighted", l: "Vol-weighted parity" },
  ];

  readonly compositeSeries = computed<readonly number[]>(() => {
    const curves = this.data.equityCurves;
    return curves[0].map((_, i) =>
      curves.reduce((acc, c) => acc + c[i], 0) / curves.length
    );
  });

  readonly hoverStat = computed(() => {
    const h = this.hoverCell();
    if (!h) return null;
    const s = this.data.heatmapStats[h.ai][h.pi];
    return {
      asset: this.data.assets[h.ai],
      period: this.data.periods[h.pi],
      ...s,
    };
  });

  readonly currentScoreLabel = computed(() => {
    const s = this.data.edgeScore[Math.floor(this.data.N / 2)];
    return (s >= 0 ? "+" : "") + s.toFixed(2);
  });
  readonly currentScoreColor = computed(() =>
    this.data.edgeScore[Math.floor(this.data.N / 2)] >= 0 ? this.TOK.bull : this.TOK.bear
  );

  protected setSplitMode(m: SplitMode): void { this.splitMode.set(m); }
  protected setComposite(c: Composite): void { this.composite.set(c); }

  protected curveDelta(curve: readonly number[]): number {
    return curve.length ? curve[curve.length - 1] - 100 : 0;
  }
  protected curveColor(curve: readonly number[]): string {
    return this.curveDelta(curve) > 0 ? this.TOK.bull : this.TOK.bear;
  }
  protected curveFill(curve: readonly number[]): string {
    return this.curveDelta(curve) > 0
      ? "rgba(38,166,154,0.10)"
      : "rgba(239,83,80,0.10)";
  }
  protected fmtPct(v: number, digits = 1): string {
    return v.toFixed(digits) + "%";
  }
  protected fmtSigned(v: number, digits = 2): string {
    return (v >= 0 ? "+" : "") + v.toFixed(digits);
  }
}
