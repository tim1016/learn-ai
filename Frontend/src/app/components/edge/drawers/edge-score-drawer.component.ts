import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
} from "@angular/core";
import { EdgeMiniLineComponent, EDGE_TOK } from "../charts/edge-charts";
import type { EdgeData } from "../services/edge-mock-data.service";

interface EdgeComponentRow {
  k: string; label: string; sub: string;
  val: number; w: number; color: string;
}

@Component({
  selector: "app-edge-score-drawer",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [EdgeMiniLineComponent],
  templateUrl: "./edge-score-drawer.component.html",
  styleUrls: ["./edge-score-drawer.component.scss"],
})
export class EdgeScoreDrawerComponent {
  data = input.required<EdgeData>();
  open = input(false);
  idx = input<number | undefined>(undefined);

  closed = output();

  protected readonly TOK = EDGE_TOK;

  protected resolvedIdx = computed(() => this.idx() ?? Math.floor(this.data().N / 2));
  protected dateLabel = computed(() => this.data().dates[this.resolvedIdx()].toISOString().slice(0, 10));
  protected composite = computed(() => this.data().edgeScore[this.resolvedIdx()]);
  protected action = computed<"LONG VOL" | "SHORT VOL" | "NO TRADE">(() => {
    const c = this.composite();
    if (c > 0.3) return "LONG VOL";
    if (c < -0.3) return "SHORT VOL";
    return "NO TRADE";
  });
  protected actionColor = computed(() => {
    const a = this.action();
    return a === "LONG VOL" ? this.TOK.bull : a === "SHORT VOL" ? this.TOK.bear : this.TOK.muted;
  });
  protected components = computed<EdgeComponentRow[]>(() => {
    const c = this.data().edgeComponents[this.resolvedIdx()];
    return [
      { k: "vrp",    label: "VRP",    sub: "tanh(−vrp_z · 0.6)",       val: c.vrp,    w: 0.4, color: "var(--score-vrp)" },
      { k: "regime", label: "Regime", sub: "state-conditioned premium", val: c.regime, w: 0.3, color: "var(--score-regime)" },
      { k: "iv",     label: "IV",     sub: "tanh((iv30 − 0.18) · 4)",   val: c.iv,     w: 0.2, color: "var(--score-iv)" },
      { k: "trend",  label: "Trend",  sub: "tanh(slope_z · 1.5)",       val: c.trend,  w: 0.1, color: "var(--score-trend)" },
    ];
  });
  protected scoreSeries = computed(() => this.data().edgeScore.slice(-60));

  protected barLeft(val: number): string {
    const pct = Math.max(-1, Math.min(1, val));
    return pct < 0 ? `${50 + pct * 50}%` : "50%";
  }
  protected barWidth(val: number): string {
    const pct = Math.max(-1, Math.min(1, val));
    return `${Math.abs(pct) * 50}%`;
  }
  protected contrib(c: EdgeComponentRow): number { return c.val * c.w; }
  protected contribColor(val: number): string {
    return val > 0 ? this.TOK.bull : val < 0 ? this.TOK.bear : this.TOK.muted;
  }
  protected fmt(v: number, digits = 3): string {
    return (v >= 0 ? "+" : "") + v.toFixed(digits);
  }
}
