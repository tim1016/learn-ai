import { ChangeDetectionStrategy, Component, computed, inject, signal } from "@angular/core";
import { toSignal } from "@angular/core/rxjs-interop";
import {
  NavigationEnd,
  Router,
  RouterLink,
  RouterLinkActive,
  RouterOutlet,
} from "@angular/router";
import { filter, map, startWith } from "rxjs";
import { PageHeaderComponent } from "../../shared/page-header/page-header.component";
import { menuLabelFor } from "../../shell/app-menu";
import { CurrentUrlService } from "../../shell/current-url.service";
import { SampleDataBannerComponent } from "../../shared/sample-data/sample-data-banner.component";
import { EdgeMiniLineComponent } from "./charts/edge-charts";
import { EdgeMockDataService } from "./services/edge-mock-data.service";
import { EdgeScoreDrawerComponent } from "./drawers/edge-score-drawer.component";
import { TradeSimDrawerComponent } from "./drawers/trade-sim-drawer.component";

interface EdgeNavCard {
  readonly route: string;
  readonly eyebrow: string;
  readonly title: string;
  readonly tagline: string;
  readonly catColor: string;
  readonly catSoft: string;
  readonly sparkValues: readonly number[];
  readonly sparkLabel: string;
  readonly sparkValue: string;
  readonly bullets: readonly string[];
}

interface CapabilityRow {
  readonly icon: string;
  readonly title: string;
  readonly subtitle: string;
  readonly badge: string;
}

@Component({
  selector: "app-edge",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink, RouterLinkActive, RouterOutlet,
    EdgeMiniLineComponent, PageHeaderComponent,
    EdgeScoreDrawerComponent, TradeSimDrawerComponent, SampleDataBannerComponent,
  ],
  templateUrl: "./edge.component.html",
  styleUrls: ["./edge.component.scss"],
})
export class EdgeComponent {
  private readonly router = inject(Router);
  private readonly mockData = inject(EdgeMockDataService);

  /** Titled by the menu entry that leads to the view (PRD #3490 R01), as Data Lab is. */
  private readonly menuNode = inject(CurrentUrlService).menuNode;
  protected readonly pageTitle = computed(() => this.menuNode()?.item.title ?? menuLabelFor("/edge"));

  readonly scoreOpen = signal(false);
  readonly tradeSimOpen = signal(false);
  readonly edgeData = computed(() => this.mockData.get());

  readonly isRoot = toSignal(
    this.router.events.pipe(
      filter((e): e is NavigationEnd => e instanceof NavigationEnd),
      map((e) => this.urlIsEdgeRoot(e.urlAfterRedirects)),
      startWith(this.urlIsEdgeRoot(this.router.url))
    ),
    { initialValue: true }
  );

  readonly cards = computed<readonly EdgeNavCard[]>(() => {
    const sparks = this.edgeData().sparklines;
    return [
      {
        route: "realized-vs-iv",
        eyebrow: "F1 · VOLATILITY",
        title: "Realized vs Implied",
        tagline: "Compare what the market expects (IV30) against what actually happened (RV).",
        catColor: "var(--ind-cat-volatility)",
        catSoft: "var(--ind-cat-volatility-soft)",
        sparkValues: sparks.vrp,
        sparkLabel: "vrp_z · 30d",
        sparkValue: `${this.fmtSigned(last(sparks.vrp))}σ`,
        bullets: [
          "Variance Risk Premium · realtime signals",
          "CtC · Parkinson · GK · Yang-Zhang estimators",
          "Coverage diagnostics for forward-blind tail",
        ],
      },
      {
        route: "cross-asset",
        eyebrow: "F2 · ROBUSTNESS",
        title: "Cross-Asset Validation",
        tagline: "Does the edge survive across SPY · QQQ · IWM · DIA and across time?",
        catColor: "var(--ind-cat-trend)",
        catSoft: "var(--ind-cat-trend-soft)",
        sparkValues: sparks.equity,
        sparkLabel: "composite equity · 30d",
        sparkValue: `${this.fmtSigned((last(sparks.equity) / sparks.equity[0] - 1) * 100, 1)}%`,
        bullets: [
          "Sharpe heatmap · asset × period × split",
          "Equal-weight + vol-weighted parity composites",
          "Deflated Sharpe Ratio · PBO via CSCV",
        ],
      },
      {
        route: "regimes",
        eyebrow: "F3 · REGIME CLUSTERING",
        title: "Market States",
        tagline: "Hand-rolled K-means and Gaussian HMM, decoded with Viterbi or posterior.",
        catColor: "var(--ind-cat-momentum)",
        catSoft: "var(--ind-cat-momentum-soft)",
        sparkValues: sparks.stability,
        sparkLabel: "stability · 30d",
        sparkValue: last(sparks.stability).toFixed(2),
        bullets: [
          "OHLCV-only or IV-extended feature space",
          "Hungarian-aligned drift · KL transition divergence",
          "Per-regime strategy fit & stability sparkline",
        ],
      },
    ];
  });

  readonly capabilities: readonly CapabilityRow[] = [
    {
      icon: "◇",
      title: "Edge Score",
      subtitle: "Composite · S^vrp·0.4 + S^regime·0.3 + S^iv·0.2 + S^trend·0.1 · weights locked",
      badge: "Inspector",
    },
    {
      icon: "↻",
      title: "Trade Simulator",
      subtitle: "Pessimistic-first · Madhavan-Smidt spread · cost attribution · is_tradable rail",
      badge: "Drawer",
    },
  ];

  protected fmtSigned(v: number, digits = 2): string {
    return (v >= 0 ? "+" : "") + v.toFixed(digits);
  }

  private urlIsEdgeRoot(url: string): boolean {
    const base = url.split("?")[0].replace(/\/$/, "");
    return base === "/edge";
  }
}

function last(values: readonly number[]): number {
  return values[values.length - 1];
}

