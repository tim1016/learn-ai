/**
 * Canvas chart primitives for the Edge feature.
 *
 * One file, eight components. Each component renders a single canvas with
 * imperative draw code; signals/inputs trigger a re-render via effect().
 * Sized via DPR so output stays crisp on hi-DPI displays.
 *
 * Ports the React/JSX prototypes in
 * `quant-trading-lab-design-system/edge_redesign/charts.jsx` to Angular 21.
 */
import {
  AfterViewInit,
  ChangeDetectionStrategy,
  Component,
  effect,
  ElementRef,
  input,
  output,
  ViewChild,
} from "@angular/core";

import type {
  EdgeData, HeatmapStat, SignalMark,
} from "../services/edge-mock-data.service";

// Shared color tokens — match _tokens.scss.
const TOK = {
  text: "#f0f3fa", subtle: "#b2b5be", muted: "#6b6f7a",
  border: "#1e222d", borderL: "#2a2e39",
  bull: "#26a69a", bear: "#ef5350", warn: "#ff9800",
  accent: "#2962ff", info: "#29b6f6",
  surface: "#131722", sunken: "#070a11", elevated: "#1b1f2e",
  vol: "#f2ad3d", trend: "#4d8dff", mom: "#a78bfa",
  reg1: "#26a69a", reg2: "#ff9800", reg3: "#ef5350",
} as const;

export const EDGE_TOK = TOK;

function setupCanvas(canvas: HTMLCanvasElement, w: number, h: number): CanvasRenderingContext2D {
  const dpr = window.devicePixelRatio || 1;
  canvas.width = w * dpr; canvas.height = h * dpr;
  canvas.style.width = w + "px"; canvas.style.height = h + "px";
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("2d canvas context unavailable");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return ctx;
}

/* ─── Mini sparkline ──────────────────────────────────────── */
@Component({
  selector: "app-edge-mini-line",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<canvas #canvas></canvas>`,
})
export class EdgeMiniLineComponent implements AfterViewInit {
  width = input.required<number>();
  height = input.required<number>();
  values = input.required<readonly number[]>();
  color = input<string>(TOK.accent);
  fill = input<string | undefined>(undefined);

  @ViewChild("canvas", { static: true }) canvasRef!: ElementRef<HTMLCanvasElement>;

  constructor() {
    effect(() => {
      const w = this.width(); const h = this.height();
      const vals = this.values(); const color = this.color(); const fill = this.fill();
      if (!this.canvasRef) return;
      this.draw(this.canvasRef.nativeElement, w, h, vals, color, fill);
    });
  }

  ngAfterViewInit(): void {
    this.draw(this.canvasRef.nativeElement, this.width(), this.height(),
      this.values(), this.color(), this.fill());
  }

  private draw(canvas: HTMLCanvasElement, w: number, h: number,
               vals: readonly number[], color: string, fill: string | undefined): void {
    const ctx = setupCanvas(canvas, w, h);
    if (vals.length < 2) return;
    const PAD = 6; const innerW = w - PAD * 2; const innerH = h - PAD * 2;
    const lo = Math.min(...vals); const hi = Math.max(...vals);
    const x = (i: number) => PAD + (i / (vals.length - 1)) * innerW;
    const y = (v: number) => PAD + innerH - ((v - lo) / Math.max(1e-9, hi - lo)) * innerH;
    if (fill) {
      ctx.fillStyle = fill;
      ctx.beginPath();
      vals.forEach((v, i) => i === 0 ? ctx.moveTo(x(i), y(v)) : ctx.lineTo(x(i), y(v)));
      ctx.lineTo(x(vals.length - 1), PAD + innerH);
      ctx.lineTo(x(0), PAD + innerH);
      ctx.closePath(); ctx.fill();
    }
    ctx.strokeStyle = color; ctx.lineWidth = 1.5;
    ctx.beginPath();
    vals.forEach((v, i) => i === 0 ? ctx.moveTo(x(i), y(v)) : ctx.lineTo(x(i), y(v)));
    ctx.stroke();
  }
}

/* ─── Price + IV dual-axis chart ──────────────────────────── */
export interface PriceIVLayers { rvBands: boolean; edgeStrip: boolean; }

@Component({
  selector: "app-edge-price-iv-chart",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div style="position: relative;">
      <canvas #canvas
        (mousemove)="onMove($event)"
        (mouseleave)="hover.emit(null)"></canvas>
    </div>
  `,
})
export class EdgePriceIVChartComponent implements AfterViewInit {
  width = input.required<number>();
  height = input.required<number>();
  data = input.required<EdgeData>();
  showOracle = input(true);
  showRealtime = input(true);
  layers = input.required<PriceIVLayers>();
  hoverIdx = input<number | null>(null);
  /** Drives the x-axis date format: ISO `YYYY-MM-DD` for daily, `MMM DD HH:mm` for intraday. */
  barSize = input<"5m" | "15m" | "1h" | "1D">("1D");

  hover = output<number | null>();

  /** Whether to render the live-IV30 marker at the right edge of the IV
   *  axis. The marker reads from `data.liveIv30`; this exposes a separate
   *  toggle for callers that want to suppress it without dropping the data. */
  showLiveIv30 = input(true);

  @ViewChild("canvas", { static: true }) canvasRef!: ElementRef<HTMLCanvasElement>;

  // Bottom padding extended to fit the dedicated x-axis tick row beneath the strip.
  private readonly PAD = { L: 44, R: 52, T: 14, B: 44 };

  constructor() {
    effect(() => {
      const _ = this.width() + this.height() + (this.hoverIdx() ?? -1);
      const __ = (this.showOracle() ? 1 : 0) + (this.showRealtime() ? 1 : 0)
                + (this.layers().rvBands ? 1 : 0) + (this.layers().edgeStrip ? 1 : 0)
                + (this.showLiveIv30() ? 1 : 0);
      void _; void __;
      const data = this.data();
      if (!this.canvasRef) return;
      this.draw(this.canvasRef.nativeElement, data);
    });
  }

  ngAfterViewInit(): void { this.draw(this.canvasRef.nativeElement, this.data()); }

  protected onMove(e: MouseEvent): void {
    const rect = (e.currentTarget as HTMLCanvasElement).getBoundingClientRect();
    const px = e.clientX - rect.left;
    const innerW = this.width() - this.PAD.L - this.PAD.R;
    const N = this.data().candles.length;
    const i = Math.round(((px - this.PAD.L) / innerW) * (N - 1));
    if (i >= 0 && i < N) this.hover.emit(i);
  }

  private draw(canvas: HTMLCanvasElement, data: EdgeData): void {
    const w = this.width(); const h = this.height();
    const { L, R, T, B } = this.PAD;
    const innerW = w - L - R; const innerH = h - T - B;
    const candles = data.candles; const N = candles.length;
    const blindStart = N - data.coverage.forward_blind_tail;

    const lo = Math.min(...candles.map(c => c.l)) * 0.998;
    const hi = Math.max(...candles.map(c => c.h)) * 1.002;

    // Right-axis range: prefer IV when available, fall back to RV when the
    // IV pipeline isn't wired (v1 live runs). With no IV at all, the IV
    // line is suppressed but RV bands and RV-YZ trace stay visible against
    // the same axis — relabel "IV30" → "RV YZ" in that case so the user
    // knows what the right axis represents.
    const ivFinite = data.iv30.filter((v) => Number.isFinite(v)) as number[];
    const rvFinite = data.rvYZ.filter((v) => Number.isFinite(v)) as number[];
    const ivAvailable = ivFinite.length > 0;
    const axisSrc = ivAvailable ? ivFinite : rvFinite;
    const ivLo = axisSrc.length ? Math.min(...axisSrc) * 0.85 : 0;
    const ivHi = axisSrc.length ? Math.max(...axisSrc) * 1.15 : 1;

    const x = (i: number) => L + (i / (N - 1)) * innerW;
    const yP = (p: number) => T + innerH - ((p - lo) / (hi - lo)) * innerH;
    const yI = (v: number) => T + innerH - ((v - ivLo) / (ivHi - ivLo)) * innerH;

    const ctx = setupCanvas(canvas, w, h);

    // Forward-blind hatch
    ctx.fillStyle = "rgba(255,152,0,0.04)";
    ctx.fillRect(x(blindStart), T, innerW - (x(blindStart) - L), innerH);
    ctx.save();
    ctx.beginPath(); ctx.rect(x(blindStart), T, innerW - (x(blindStart) - L), innerH); ctx.clip();
    ctx.strokeStyle = "rgba(255,152,0,0.10)"; ctx.lineWidth = 1;
    for (let dx = -innerH; dx < innerW + innerH; dx += 6) {
      ctx.beginPath();
      ctx.moveTo(x(blindStart) + dx, T);
      ctx.lineTo(x(blindStart) + dx + innerH, T + innerH);
      ctx.stroke();
    }
    ctx.restore();

    // Grid + axis labels
    ctx.strokeStyle = TOK.border; ctx.lineWidth = 1;
    ctx.font = "10px JetBrains Mono, monospace";
    for (let g = 0; g < 5; g++) {
      const yy = T + (g / 4) * innerH;
      ctx.beginPath(); ctx.moveTo(L, yy + 0.5); ctx.lineTo(L + innerW, yy + 0.5); ctx.stroke();
      const pVal = lo + (1 - g / 4) * (hi - lo);
      ctx.fillStyle = TOK.muted; ctx.textAlign = "right";
      ctx.fillText(pVal.toFixed(0), L - 6, yy + 3);
      const iVal = ivLo + (1 - g / 4) * (ivHi - ivLo);
      ctx.fillStyle = TOK.vol; ctx.textAlign = "left";
      ctx.fillText((iVal * 100).toFixed(0) + "%", L + innerW + 6, yy + 3);
    }

    // Candles
    const cw = Math.max(1.5, innerW / N * 0.7);
    candles.forEach((c, i) => {
      const cx = x(i); const up = c.c >= c.o;
      ctx.strokeStyle = up ? TOK.bull : TOK.bear;
      ctx.fillStyle = up ? TOK.bull : TOK.bear;
      ctx.beginPath(); ctx.moveTo(cx, yP(c.h)); ctx.lineTo(cx, yP(c.l)); ctx.stroke();
      ctx.fillRect(cx - cw / 2, Math.min(yP(c.o), yP(c.c)), cw, Math.max(1, Math.abs(yP(c.c) - yP(c.o))));
    });

    // IV30 — only when the live IV pipeline supplied data; otherwise the
    // right axis is repurposed for RV (see ivAvailable above).
    if (ivAvailable) {
      ctx.strokeStyle = TOK.vol; ctx.lineWidth = 1.5;
      ctx.beginPath();
      data.iv30.forEach((v, i) => {
        if (!Number.isFinite(v)) return;
        const py = yI(v);
        if (i === 0) ctx.moveTo(x(i), py); else ctx.lineTo(x(i), py);
      });
      ctx.stroke();
    }

    // Live IV30 marker — anchored at the rightmost x within the data domain
    // (no axis extension; per scoping decision (b) the marker lives in
    // existing pixels). Renders only when callers supply data.liveIv30 AND
    // its value falls inside the right-axis window — out-of-range values
    // would draw off the chart and mislead the eye.
    const live = data.liveIv30;
    if (this.showLiveIv30() && live && Number.isFinite(live.iv30Act365)
        && live.iv30Act365 >= ivLo && live.iv30Act365 <= ivHi) {
      const cx = L + innerW;
      const cy = yI(live.iv30Act365);
      // Soft halo + filled dot in the IV-axis color.
      ctx.fillStyle = "rgba(242, 173, 61, 0.20)";
      ctx.beginPath(); ctx.arc(cx, cy, 6, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = TOK.vol;
      ctx.beginPath(); ctx.arc(cx, cy, 3, 0, Math.PI * 2); ctx.fill();
      // "LIVE n.n%" badge directly above the dot — stays inside the canvas
      // but doesn't fight the axis labels in the right margin.
      const pct = (live.iv30Act365 * 100).toFixed(1) + "%";
      const label = `LIVE ${pct}`;
      ctx.font = "9px JetBrains Mono, monospace";
      ctx.textAlign = "center"; ctx.textBaseline = "alphabetic";
      // Pill backdrop for legibility against price candles.
      const padX = 4; const padY = 2;
      const tw = ctx.measureText(label).width;
      const bx = cx - tw / 2 - padX;
      const by = cy - 16 - padY;
      ctx.fillStyle = "rgba(11,14,20,0.85)";
      ctx.fillRect(bx, by, tw + padX * 2, 12 + padY);
      ctx.fillStyle = TOK.vol;
      ctx.fillText(label, cx, cy - 8);
    }

    // RV bands
    if (this.layers().rvBands) {
      [{ scale: 1.06, alpha: 0.45 }, { scale: 1.0, alpha: 0.6 }, { scale: 0.94, alpha: 0.85 }].forEach((s) => {
        ctx.strokeStyle = `rgba(242, 173, 61, ${s.alpha})`;
        ctx.lineWidth = 1; ctx.setLineDash([]);
        ctx.beginPath();
        data.rvYZ.forEach((v, i) => {
          const py = yI(v * s.scale);
          if (i === 0) ctx.moveTo(x(i), py); else ctx.lineTo(x(i), py);
        });
        ctx.stroke();
      });
    }

    // Edge Score strip — overlay near the bottom of the chart area, with
    // breathing room above (so it isn't flush against the lowest price
    // gridline) and a left-edge label + right-edge current-value chip
    // so the colormap encoding is self-explanatory.
    if (this.layers().edgeStrip) {
      const stripH = 16; const stripY = T + innerH - stripH - 8;
      // Backdrop with a soft top border to separate from candle area.
      ctx.fillStyle = "rgba(11,14,20,0.78)";
      ctx.fillRect(L, stripY, innerW, stripH);
      const ww = innerW / N;
      data.edgeScore.forEach((s, i) => {
        const col = s > 0
          ? `rgba(38,166,154,${Math.min(1, Math.abs(s) * 1.4)})`
          : `rgba(239,83,80,${Math.min(1, Math.abs(s) * 1.4)})`;
        ctx.fillStyle = col;
        ctx.fillRect(x(i) - ww / 2, stripY, Math.max(1, ww), stripH);
      });
      ctx.strokeStyle = TOK.borderL; ctx.lineWidth = 1;
      ctx.strokeRect(L + 0.5, stripY + 0.5, innerW - 1, stripH - 1);

      // Left-edge "EDGE" label inside a small contrast pill.
      ctx.fillStyle = "rgba(11,14,20,0.92)";
      ctx.fillRect(L + 1, stripY + 1, 38, stripH - 2);
      ctx.fillStyle = TOK.subtle; ctx.font = "9px JetBrains Mono, monospace";
      ctx.textAlign = "left"; ctx.textBaseline = "middle";
      ctx.fillText("EDGE", L + 5, stripY + stripH / 2 + 0.5);

      // Right-edge current-value chip — driven by hover, falls back to last bar.
      const hi = this.hoverIdx();
      const curIdx = (hi != null && hi >= 0 && hi < N) ? hi : N - 1;
      const curScore = data.edgeScore[curIdx] ?? 0;
      const chipText = (curScore >= 0 ? "+" : "") + curScore.toFixed(2);
      const chipColor = curScore > 0 ? TOK.bull : curScore < 0 ? TOK.bear : TOK.subtle;
      const chipW = 44; const chipX = L + innerW - chipW - 1;
      ctx.fillStyle = "rgba(11,14,20,0.92)";
      ctx.fillRect(chipX, stripY + 1, chipW, stripH - 2);
      ctx.fillStyle = chipColor; ctx.font = "10px JetBrains Mono, monospace";
      ctx.textAlign = "right";
      ctx.fillText(chipText, L + innerW - 4, stripY + stripH / 2 + 0.5);
      ctx.textBaseline = "alphabetic";
    }

    // Signals
    const sigs: (SignalMark & { oracle: boolean })[] = [
      ...(this.showRealtime() ? data.signals.map(s => ({ ...s, oracle: false })) : []),
      ...(this.showOracle()   ? data.oracleSignals.map(s => ({ ...s, oracle: true })) : []),
    ];
    sigs.forEach(s => {
      const cx = x(s.i); const up = s.dir === "long";
      const cy = up ? yP(candles[s.i].l) + 16 : yP(candles[s.i].h) - 16;
      ctx.fillStyle = up ? TOK.bull : TOK.bear;
      const sz = 5;
      ctx.beginPath();
      if (up) {
        ctx.moveTo(cx, cy - sz); ctx.lineTo(cx - sz, cy + sz); ctx.lineTo(cx + sz, cy + sz);
      } else {
        ctx.moveTo(cx, cy + sz); ctx.lineTo(cx - sz, cy - sz); ctx.lineTo(cx + sz, cy - sz);
      }
      ctx.closePath();
      if (s.oracle) {
        ctx.setLineDash([2, 2]);
        ctx.strokeStyle = up ? TOK.bull : TOK.bear; ctx.lineWidth = 1.4;
        ctx.stroke(); ctx.setLineDash([]);
      } else {
        ctx.fill();
      }
    });

    // Crosshair
    const hi2 = this.hoverIdx();
    if (hi2 != null && hi2 >= 0 && hi2 < N) {
      const cx = x(hi2);
      ctx.strokeStyle = TOK.borderL; ctx.setLineDash([3, 3]); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(cx, T); ctx.lineTo(cx, T + innerH); ctx.stroke();
      ctx.setLineDash([]);
    }

    // Axis hints
    ctx.fillStyle = TOK.muted; ctx.textAlign = "left";
    ctx.fillText("PRICE", L, T - 2);
    ctx.fillStyle = TOK.vol; ctx.textAlign = "right";
    ctx.fillText(ivAvailable ? "IV30" : "RV YZ", L + innerW, T - 2);

    // ── Bottom date axis ───────────────────────────────────────────────
    // Adaptive format: ISO YYYY-MM-DD for daily, MMM DD HH:mm for intraday.
    // Pick ~7 ticks evenly across the available width.
    if (data.dates && data.dates.length) {
      const targetTicks = Math.max(4, Math.min(8, Math.floor(innerW / 110)));
      const stride = Math.max(1, Math.floor((N - 1) / targetTicks));
      const isDaily = this.barSize() === "1D";
      ctx.fillStyle = TOK.muted; ctx.font = "10px JetBrains Mono, monospace";
      ctx.textAlign = "center"; ctx.textBaseline = "alphabetic";
      const labelY = T + innerH + 16;
      // Tick stub line
      ctx.strokeStyle = TOK.borderL; ctx.lineWidth = 1;
      for (let i = 0; i < N; i += stride) {
        const px = x(i);
        if (px < L + 30 || px > L + innerW - 30) continue;
        ctx.beginPath();
        ctx.moveTo(px, T + innerH + 1);
        ctx.lineTo(px, T + innerH + 5);
        ctx.stroke();
        ctx.fillText(formatBarDate(data.dates[i], isDaily), px, labelY);
      }
    }
  }
}

function formatBarDate(d: Date, isDaily: boolean): string {
  if (isDaily) {
    return d.toISOString().slice(0, 10); // YYYY-MM-DD
  }
  const monthShort = d.toLocaleString("en-US", { month: "short", timeZone: "America/New_York" });
  const day = d.toLocaleString("en-US", { day: "2-digit", timeZone: "America/New_York" });
  const time = d.toLocaleString("en-US", {
    hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "America/New_York",
  });
  return `${monthShort} ${day} ${time}`;
}

/* ─── VRP histogram ───────────────────────────────────────── */
@Component({
  selector: "app-edge-vrp-histogram",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<canvas #canvas></canvas>`,
})
export class EdgeVrpHistogramComponent implements AfterViewInit {
  width = input.required<number>();
  height = input.required<number>();
  data = input.required<EdgeData>();
  currentIdx = input.required<number>();

  @ViewChild("canvas", { static: true }) canvasRef!: ElementRef<HTMLCanvasElement>;

  constructor() {
    effect(() => {
      const _ = this.width() + this.height() + this.currentIdx();
      void _;
      const d = this.data();
      if (!this.canvasRef) return;
      this.draw(this.canvasRef.nativeElement, d);
    });
  }
  ngAfterViewInit(): void { this.draw(this.canvasRef.nativeElement, this.data()); }

  private draw(canvas: HTMLCanvasElement, data: EdgeData): void {
    const w = this.width(); const h = this.height();
    const PAD_L = 36, PAD_R = 12, PAD_T = 8, PAD_B = 24;
    const innerW = w - PAD_L - PAD_R; const innerH = h - PAD_T - PAD_B;
    const ctx = setupCanvas(canvas, w, h);
    const bins = data.vrpHistogram;
    if (!bins.length) {
      // Empty state — IV pipeline not yet wired (v1) so no VRP samples.
      ctx.fillStyle = TOK.muted;
      ctx.font = "11px JetBrains Mono, monospace";
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText(
        "No VRP samples — IV pipeline not yet wired (v1).",
        w / 2, h / 2 - 6,
      );
      ctx.fillStyle = TOK.subtle; ctx.font = "10px JetBrains Mono, monospace";
      ctx.fillText(
        "Returns once /api/edge/realized-vs-iv reads OptionIvSnapshots.",
        w / 2, h / 2 + 10,
      );
      ctx.textBaseline = "alphabetic";
      return;
    }
    const maxC = Math.max(...bins.map(b => b.count));
    const minX = bins[0].x0, maxX = bins[bins.length - 1].x1;
    const xS = (v: number) => PAD_L + ((v - minX) / (maxX - minX)) * innerW;
    const yS = (c: number) => PAD_T + innerH - (c / maxC) * innerH;

    let cum = 0; const total = bins.reduce((a, b) => a + b.count, 0);
    bins.forEach(b => {
      const start = cum / total; cum += b.count;
      const end = cum / total;
      const inside = end >= 0.05 && start <= 0.95;
      ctx.fillStyle = inside ? "rgba(41,98,255,0.45)" : "rgba(41,98,255,0.18)";
      const bx = xS(b.x0); const bw = xS(b.x1) - xS(b.x0) - 1;
      ctx.fillRect(bx, yS(b.count), bw, innerH - (yS(b.count) - PAD_T));
    });

    const x0 = xS(0);
    ctx.strokeStyle = TOK.borderL; ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(x0, PAD_T); ctx.lineTo(x0, PAD_T + innerH); ctx.stroke();
    ctx.setLineDash([]);

    const cur = data.vrpForward[this.currentIdx()];
    if (!Number.isNaN(cur)) {
      const cx = xS(cur);
      ctx.strokeStyle = TOK.warn; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(cx, PAD_T - 2); ctx.lineTo(cx, PAD_T + innerH); ctx.stroke();
      ctx.fillStyle = TOK.warn;
      ctx.beginPath();
      ctx.moveTo(cx, PAD_T - 2); ctx.lineTo(cx - 4, PAD_T - 8); ctx.lineTo(cx + 4, PAD_T - 8);
      ctx.closePath(); ctx.fill();
    }

    // Bottom % tick labels — descriptor moved out of the canvas into the
    // panel header in the parent template (no more text-on-text overlap).
    ctx.fillStyle = TOK.muted; ctx.font = "10px JetBrains Mono"; ctx.textAlign = "center";
    [-0.1, -0.05, 0, 0.05, 0.1].forEach(v => {
      if (v >= minX && v <= maxX) {
        ctx.fillText((v * 100).toFixed(0) + "%", xS(v), PAD_T + innerH + 16);
      }
    });
  }
}

/* ─── Sharpe heatmap ──────────────────────────────────────── */
export interface HeatmapHover { ai: number; pi: number; }

@Component({
  selector: "app-edge-sharpe-heatmap",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<canvas #canvas
    (mousemove)="onMove($event)"
    (mouseleave)="hover.emit(null)"></canvas>`,
})
export class EdgeSharpeHeatmapComponent implements AfterViewInit {
  width = input.required<number>();
  height = input.required<number>();
  data = input.required<EdgeData>();
  hoverCell = input<HeatmapHover | null>(null);

  hover = output<HeatmapHover | null>();

  @ViewChild("canvas", { static: true }) canvasRef!: ElementRef<HTMLCanvasElement>;
  private readonly PAD = { L: 56, R: 12, T: 26, B: 8 };

  constructor() {
    effect(() => {
      const _ = this.width() + this.height(); void _;
      const cell = this.hoverCell(); void cell;
      if (!this.canvasRef) return;
      this.draw(this.canvasRef.nativeElement, this.data());
    });
  }
  ngAfterViewInit(): void { this.draw(this.canvasRef.nativeElement, this.data()); }

  protected onMove(e: MouseEvent): void {
    const rect = (e.currentTarget as HTMLCanvasElement).getBoundingClientRect();
    const px = e.clientX - rect.left; const py = e.clientY - rect.top;
    const data = this.data();
    const A = data.assets.length; const P = data.periods.length;
    const cellW = (this.width() - this.PAD.L - this.PAD.R) / P;
    const cellH = (this.height() - this.PAD.T - this.PAD.B) / A;
    const pi = Math.floor((px - this.PAD.L) / cellW);
    const ai = Math.floor((py - this.PAD.T) / cellH);
    if (pi >= 0 && pi < P && ai >= 0 && ai < A) this.hover.emit({ ai, pi });
    else this.hover.emit(null);
  }

  private draw(canvas: HTMLCanvasElement, data: EdgeData): void {
    const w = this.width(); const h = this.height();
    const A = data.assets.length; const P = data.periods.length;
    const cellW = (w - this.PAD.L - this.PAD.R) / P;
    const cellH = (h - this.PAD.T - this.PAD.B) / A;
    const ctx = setupCanvas(canvas, w, h);

    const color = (s: number) => {
      const t = Math.max(-1, Math.min(1, s / 1.5));
      if (t > 0) return `rgba(38,166,154,${0.18 + t * 0.65})`;
      if (t < 0) return `rgba(239,83,80,${0.18 + Math.abs(t) * 0.65})`;
      return "rgba(150,150,150,0.10)";
    };

    ctx.font = "10px JetBrains Mono";
    ctx.fillStyle = TOK.muted; ctx.textAlign = "center";
    data.periods.forEach((p, pi) => {
      ctx.fillText(p, this.PAD.L + cellW * (pi + 0.5), this.PAD.T - 8);
    });
    ctx.textAlign = "right";
    data.assets.forEach((a, ai) => {
      ctx.fillStyle = TOK.subtle; ctx.font = "bold 11px JetBrains Mono";
      ctx.fillText(a, this.PAD.L - 10, this.PAD.T + cellH * (ai + 0.5) + 4);
    });
    const hov = this.hoverCell();
    data.sharpeMatrix.forEach((row, ai) => {
      row.forEach((s, pi) => {
        const x = this.PAD.L + cellW * pi; const y = this.PAD.T + cellH * ai;
        ctx.fillStyle = color(s);
        ctx.fillRect(x + 1, y + 1, cellW - 2, cellH - 2);
        if (s > 1.0) {
          ctx.strokeStyle = "rgba(38,166,154,0.85)"; ctx.lineWidth = 1.5;
          ctx.strokeRect(x + 1.5, y + 1.5, cellW - 3, cellH - 3);
        }
        ctx.fillStyle = "#f0f3fa"; ctx.font = "11px JetBrains Mono"; ctx.textAlign = "center";
        ctx.fillText(s.toFixed(2), x + cellW / 2, y + cellH / 2 + 4);
        if (s < -0.5) {
          ctx.fillStyle = TOK.bear;
          ctx.beginPath(); ctx.arc(x + cellW - 6, y + 6, 2.5, 0, Math.PI * 2); ctx.fill();
        }
        if (hov && hov.ai === ai && hov.pi === pi) {
          ctx.strokeStyle = TOK.accent; ctx.lineWidth = 2;
          ctx.strokeRect(x + 1.5, y + 1.5, cellW - 3, cellH - 3);
        }
      });
    });
  }
}

/* ─── Regime price chart ──────────────────────────────────── */
@Component({
  selector: "app-edge-regime-price-chart",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<canvas #canvas
    (mousemove)="onMove($event)"
    (mouseleave)="hover.emit(null)"></canvas>`,
})
export class EdgeRegimePriceChartComponent implements AfterViewInit {
  width = input.required<number>();
  height = input.required<number>();
  data = input.required<EdgeData>();
  viewMode = input<"viterbi" | "posterior">("viterbi");
  hoverIdx = input<number | null>(null);

  hover = output<number | null>();

  @ViewChild("canvas", { static: true }) canvasRef!: ElementRef<HTMLCanvasElement>;
  private readonly PAD = { L: 40, R: 12, T: 24, B: 22 };

  constructor() {
    effect(() => {
      const _ = this.width() + this.height() + (this.hoverIdx() ?? -1);
      void _; void this.viewMode();
      if (!this.canvasRef) return;
      this.draw(this.canvasRef.nativeElement, this.data());
    });
  }
  ngAfterViewInit(): void { this.draw(this.canvasRef.nativeElement, this.data()); }

  protected onMove(e: MouseEvent): void {
    const rect = (e.currentTarget as HTMLCanvasElement).getBoundingClientRect();
    const px = e.clientX - rect.left;
    const innerW = this.width() - this.PAD.L - this.PAD.R;
    const N = this.data().candles.length;
    const i = Math.round(((px - this.PAD.L) / innerW) * (N - 1));
    if (i >= 0 && i < N) this.hover.emit(i); else this.hover.emit(null);
  }

  private draw(canvas: HTMLCanvasElement, data: EdgeData): void {
    const w = this.width(); const h = this.height();
    const { L, R, T, B } = this.PAD;
    const innerW = w - L - R; const innerH = h - T - B;
    const closes = data.candles.map(c => c.c);
    const lo = Math.min(...closes) * 0.998; const hi = Math.max(...closes) * 1.002;
    const N = closes.length;
    const x = (i: number) => L + (i / (N - 1)) * innerW;
    const y = (v: number) => T + innerH - ((v - lo) / (hi - lo)) * innerH;
    const colors = [TOK.reg1, TOK.reg2, TOK.reg3];
    const ctx = setupCanvas(canvas, w, h);

    if (this.viewMode() === "viterbi") {
      const ww = innerW / N;
      data.regimePath.forEach((reg, i) => {
        ctx.fillStyle = `${colors[reg]}26`;
        ctx.fillRect(x(i) - ww / 2, T, Math.max(1, ww + 1), innerH);
      });
    } else {
      const ww = innerW / N;
      data.regimePath.forEach((reg, i) => {
        [0, 1, 2].forEach((rIdx, ri) => {
          const p = rIdx === reg ? 0.7 : (Math.abs(rIdx - reg) === 1 ? 0.2 : 0.1);
          ctx.fillStyle = `${colors[rIdx]}${Math.floor(p * 60).toString(16).padStart(2, "0")}`;
          ctx.fillRect(x(i) - ww / 2, T + (ri / 3) * innerH, Math.max(1, ww + 1), innerH / 3);
        });
      });
    }

    ctx.strokeStyle = TOK.border; ctx.lineWidth = 1;
    for (let g = 0; g < 5; g++) {
      const yy = T + (g / 4) * innerH;
      ctx.beginPath(); ctx.moveTo(L, yy + 0.5); ctx.lineTo(L + innerW, yy + 0.5); ctx.stroke();
      ctx.fillStyle = TOK.muted; ctx.font = "10px JetBrains Mono"; ctx.textAlign = "right";
      ctx.fillText((lo + (1 - g / 4) * (hi - lo)).toFixed(0), L - 6, yy + 3);
    }

    ctx.strokeStyle = TOK.text; ctx.lineWidth = 1.4;
    ctx.beginPath();
    closes.forEach((v, i) => i === 0 ? ctx.moveTo(x(i), y(v)) : ctx.lineTo(x(i), y(v)));
    ctx.stroke();

    const hi2 = this.hoverIdx();
    if (hi2 != null) {
      ctx.strokeStyle = TOK.borderL; ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(x(hi2), T); ctx.lineTo(x(hi2), T + innerH); ctx.stroke();
      ctx.setLineDash([]);
    }

    ctx.fillStyle = TOK.subtle; ctx.font = "10px JetBrains Mono"; ctx.textAlign = "left";
    ctx.fillText("SPY · 1D · regime-tinted (3-state HMM)", L, T - 8);
  }
}

/* ─── Transition matrix ───────────────────────────────────── */
@Component({
  selector: "app-edge-transition-matrix",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<canvas #canvas></canvas>`,
})
export class EdgeTransitionMatrixComponent implements AfterViewInit {
  size = input.required<number>();
  matrix = input.required<readonly (readonly number[])[]>();

  @ViewChild("canvas", { static: true }) canvasRef!: ElementRef<HTMLCanvasElement>;

  constructor() {
    effect(() => {
      const _ = this.size(); void _;
      const m = this.matrix(); void m;
      if (!this.canvasRef) return;
      this.draw(this.canvasRef.nativeElement);
    });
  }
  ngAfterViewInit(): void { this.draw(this.canvasRef.nativeElement); }

  private draw(canvas: HTMLCanvasElement): void {
    const size = this.size(); const matrix = this.matrix();
    const PAD = 30; const cell = (size - PAD * 2) / matrix.length;
    const ctx = setupCanvas(canvas, size, size);
    ctx.font = "10px JetBrains Mono";
    matrix.forEach((row, i) => row.forEach((v, j) => {
      ctx.fillStyle = `rgba(41,98,255,${0.10 + v * 0.65})`;
      ctx.fillRect(PAD + j * cell, PAD + i * cell, cell - 2, cell - 2);
      ctx.fillStyle = v > 0.4 ? "#fff" : TOK.subtle;
      ctx.font = v > 0.4 ? "bold 11px JetBrains Mono" : "10px JetBrains Mono";
      ctx.textAlign = "center";
      ctx.fillText(v.toFixed(2), PAD + j * cell + cell / 2, PAD + i * cell + cell / 2 + 4);
    }));
    ctx.fillStyle = TOK.muted; ctx.font = "10px JetBrains Mono"; ctx.textAlign = "center";
    ["S0", "S1", "S2"].forEach((s, j) => ctx.fillText(s, PAD + j * cell + cell / 2, PAD - 8));
    ctx.textAlign = "right";
    ["S0", "S1", "S2"].forEach((s, i) => ctx.fillText(s, PAD - 6, PAD + i * cell + cell / 2 + 4));
    ctx.textAlign = "left"; ctx.fillStyle = TOK.subtle;
    ctx.fillText("TRANSITION MATRIX", PAD - 22, 14);
  }
}

/* ─── Per-regime feature radar ────────────────────────────── */
@Component({
  selector: "app-edge-regime-radar",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<canvas #canvas></canvas>`,
})
export class EdgeRegimeRadarComponent implements AfterViewInit {
  size = input.required<number>();
  axes = input.required<readonly string[]>();
  regimeData = input.required<readonly (readonly number[])[]>();

  @ViewChild("canvas", { static: true }) canvasRef!: ElementRef<HTMLCanvasElement>;

  constructor() {
    effect(() => {
      const _ = this.size(); void _;
      void this.axes(); void this.regimeData();
      if (!this.canvasRef) return;
      this.draw(this.canvasRef.nativeElement);
    });
  }
  ngAfterViewInit(): void { this.draw(this.canvasRef.nativeElement); }

  private draw(canvas: HTMLCanvasElement): void {
    const size = this.size(); const axes = this.axes(); const data = this.regimeData();
    const PAD = 30; const cx = size / 2; const cy = size / 2;
    const R = (size - PAD * 2) / 2;
    const colors = [TOK.reg1, TOK.reg2, TOK.reg3];
    const ctx = setupCanvas(canvas, size, size);

    ctx.strokeStyle = TOK.border; ctx.lineWidth = 1;
    [0.25, 0.5, 0.75, 1].forEach(rr => {
      ctx.beginPath();
      axes.forEach((_, i) => {
        const a = (i / axes.length) * Math.PI * 2 - Math.PI / 2;
        const x = cx + Math.cos(a) * R * rr; const y = cy + Math.sin(a) * R * rr;
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.closePath(); ctx.stroke();
    });

    ctx.fillStyle = TOK.muted; ctx.font = "9px JetBrains Mono"; ctx.textAlign = "center";
    axes.forEach((label, i) => {
      const a = (i / axes.length) * Math.PI * 2 - Math.PI / 2;
      const x = cx + Math.cos(a) * R; const y = cy + Math.sin(a) * R;
      ctx.strokeStyle = TOK.border;
      ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(x, y); ctx.stroke();
      const lx = cx + Math.cos(a) * (R + 14); const ly = cy + Math.sin(a) * (R + 14);
      ctx.fillText(label, lx, ly + 3);
    });

    data.forEach((vals, ri) => {
      ctx.fillStyle = colors[ri] + "24";
      ctx.strokeStyle = colors[ri]; ctx.lineWidth = 1.6;
      ctx.beginPath();
      vals.forEach((v, i) => {
        const a = (i / vals.length) * Math.PI * 2 - Math.PI / 2;
        const rr = R * v;
        const x = cx + Math.cos(a) * rr; const y = cy + Math.sin(a) * rr;
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.closePath(); ctx.fill(); ctx.stroke();
    });
  }
}

/* ─── Stability sparkline ─────────────────────────────────── */
@Component({
  selector: "app-edge-stability-sparkline",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<canvas #canvas></canvas>`,
})
export class EdgeStabilitySparklineComponent implements AfterViewInit {
  width = input.required<number>();
  height = input.required<number>();
  data = input.required<readonly number[]>();

  @ViewChild("canvas", { static: true }) canvasRef!: ElementRef<HTMLCanvasElement>;

  constructor() {
    effect(() => {
      const _ = this.width() + this.height(); void _;
      void this.data();
      if (!this.canvasRef) return;
      this.draw(this.canvasRef.nativeElement);
    });
  }
  ngAfterViewInit(): void { this.draw(this.canvasRef.nativeElement); }

  private draw(canvas: HTMLCanvasElement): void {
    const w = this.width(); const h = this.height();
    const data = this.data();
    const PAD_L = 40, PAD_R = 8, PAD_T = 14, PAD_B = 18;
    const innerW = w - PAD_L - PAD_R; const innerH = h - PAD_T - PAD_B;
    const lo = Math.min(...data); const hi = Math.max(...data);
    const ctx = setupCanvas(canvas, w, h);

    ctx.strokeStyle = TOK.border; ctx.setLineDash([2, 4]); ctx.lineWidth = 1;
    const yT = PAD_T + innerH - ((0.5 - lo) / (hi - lo)) * innerH;
    ctx.beginPath(); ctx.moveTo(PAD_L, yT); ctx.lineTo(PAD_L + innerW, yT); ctx.stroke();
    ctx.setLineDash([]);

    ctx.strokeStyle = TOK.mom; ctx.lineWidth = 1.4;
    ctx.beginPath();
    data.forEach((v, i) => {
      const x = PAD_L + (i / (data.length - 1)) * innerW;
      const y = PAD_T + innerH - ((v - lo) / (hi - lo)) * innerH;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();

    data.forEach((v, i) => {
      if (v < 0.5) {
        const x = PAD_L + (i / (data.length - 1)) * innerW;
        const y = PAD_T + innerH - ((v - lo) / (hi - lo)) * innerH;
        ctx.fillStyle = TOK.bear;
        ctx.beginPath(); ctx.arc(x, y, 2.5, 0, Math.PI * 2); ctx.fill();
      }
    });

    ctx.fillStyle = TOK.subtle; ctx.font = "10px JetBrains Mono"; ctx.textAlign = "left";
    ctx.fillText("REGIME STABILITY · structural-break risk below 0.50", PAD_L, PAD_T - 4);
    ctx.fillStyle = TOK.muted; ctx.textAlign = "right";
    ctx.fillText(data[data.length - 1].toFixed(2), PAD_L + innerW, PAD_T + innerH - 1);
  }
}

export type EdgeHeatmapStat = HeatmapStat;
