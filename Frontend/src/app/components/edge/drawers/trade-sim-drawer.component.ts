import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
} from "@angular/core";
import { EdgeMiniLineComponent, EDGE_TOK } from "../charts/edge-charts";
import type { EdgeData } from "../services/edge-mock-data.service";

interface CostRow {
  k: string; v: number; c: string; bold: boolean;
}

@Component({
  selector: "app-trade-sim-drawer",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [EdgeMiniLineComponent],
  templateUrl: "./trade-sim-drawer.component.html",
  styleUrls: ["./trade-sim-drawer.component.scss"],
})
export class TradeSimDrawerComponent {
  data = input.required<EdgeData>();
  open = input(false);

  closed = output();

  protected readonly TOK = EDGE_TOK;

  protected tradableCount = computed(() =>
    this.data().trades.filter((t) => t.tradable).length
  );

  protected equitySeries = computed(() => this.data().equityFromTrades);

  protected equityFinal = computed(() => {
    const eq = this.equitySeries();
    return eq.length ? eq[eq.length - 1] - 100 : 0;
  });

  protected costRows = computed<CostRow[]>(() => {
    const ca = this.data().costAttribution;
    return [
      { k: "gross_pnl",      v: ca.gross,       c: this.TOK.bull, bold: false },
      { k: "spread_cost",    v: ca.spread,      c: this.TOK.bear, bold: false },
      { k: "slippage",       v: ca.slip,        c: this.TOK.bear, bold: false },
      { k: "commissions",    v: ca.comm,        c: this.TOK.bear, bold: false },
      { k: "net_pnl",        v: ca.net,         c: ca.net > 0 ? this.TOK.bull : this.TOK.bear, bold: true },
      { k: "net (tradable)", v: ca.netTradable, c: ca.netTradable > 0 ? this.TOK.bull : this.TOK.bear, bold: true },
    ];
  });

  protected costMax = computed(() => {
    const ca = this.data().costAttribution;
    return Math.max(Math.abs(ca.gross), Math.abs(ca.spread + ca.slip + ca.comm), 1);
  });

  protected fmtSign(v: number, digits = 1): string {
    return (v >= 0 ? "+" : "") + v.toFixed(digits);
  }

  protected costBarLeft(v: number): string {
    return v < 0 ? `${50 + (v / this.costMax()) * 50}%` : "50%";
  }
  protected costBarWidth(v: number): string {
    return `${Math.abs(v / this.costMax()) * 50}%`;
  }

  protected sideClass(side: string): string {
    return side === "LONG VOL" ? "long" : "short";
  }
  protected sideColor(side: string): string {
    return side === "LONG VOL" ? this.TOK.bull : this.TOK.bear;
  }
  protected netColor(net: number): string {
    return net > 0 ? this.TOK.bull : this.TOK.bear;
  }
  protected tradableColor(t: boolean): string {
    return t ? this.TOK.bull : this.TOK.warn;
  }
  protected netOf(t: { gross: number; spread: number; slip: number; comm: number }): number {
    return t.gross + t.spread + t.slip + t.comm;
  }
}
