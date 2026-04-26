import { Injectable } from "@angular/core";

export interface Candle { o: number; h: number; l: number; c: number; v: number; }
export interface SignalMark { i: number; dir: "long" | "short"; }
export interface EdgeComponents { vrp: number; regime: number; iv: number; trend: number; }
export interface VrpBin { x0: number; x1: number; count: number; }
export interface HeatmapStat { sharpe: number; n_trades: number; win_rate: number; max_dd: number; }
export interface PerRegime { id: number; name: string; sharpe: number; pnl: number; }
export interface Trade {
  n: number; ts: string; side: "LONG VOL" | "SHORT VOL";
  entry: number; exit: number; hold: number;
  gross: number; spread: number; slip: number; comm: number; tradable: boolean;
}
export interface CostAttribution {
  gross: number; spread: number; slip: number; comm: number; net: number; netTradable: number;
}

export interface EdgeData {
  N: number;
  dates: Date[];
  candles: Candle[];
  iv30: number[];
  ivVol: number[];
  skew: number[];
  termSlope: number[];
  rvCloseClose: number[];
  rvParkinson: number[];
  rvGK: number[];
  rvYZ: number[];
  rvForward: number[];
  vrpForward: number[];
  vrpZ: number[];
  vrpHistogram: VrpBin[];
  regimePath: number[];
  transitionMatrix: number[][];
  edgeScore: number[];
  edgeComponents: EdgeComponents[];
  signals: SignalMark[];
  oracleSignals: SignalMark[];
  coverage: { bars_total: number; forward_blind_tail: number; iv_first_ts: string; };
  assets: string[];
  periods: string[];
  sharpeMatrix: number[][];
  heatmapStats: HeatmapStat[][];
  equityCurves: number[][];
  perRegime: PerRegime[];
  stability: number[];
  trades: Trade[];
  equityFromTrades: number[];
  costAttribution: CostAttribution;
  sparklines: { vrp: number[]; equity: number[]; stability: number[]; };
}

/** Synthetic but shape-correct Edge data — matches the contract the live
 *  /api/edge/* endpoints return, so the UI can render before the wire-up
 *  layer lands. Replaces window.EDGE_DATA from the design prototype. */
@Injectable({ providedIn: "root" })
export class EdgeMockDataService {
  private cached: EdgeData | null = null;

  get(): EdgeData {
    if (!this.cached) this.cached = this.build();
    return this.cached;
  }

  private build(): EdgeData {
    const N = 250;
    const r = mulberry32(20260424);
    const regimePath: number[] = [];
    let regime = 0;
    for (let i = 0; i < N; i++) {
      if (i > 0) {
        const u = r();
        if (regime === 0) regime = u < 0.95 ? 0 : (u < 0.985 ? 2 : 1);
        else if (regime === 1) regime = u < 0.92 ? 1 : (u < 0.97 ? 0 : 2);
        else regime = u < 0.93 ? 2 : (u < 0.985 ? 1 : 0);
      }
      regimePath.push(regime);
    }

    let price = 542.18;
    const candles: Candle[] = [];
    const rvCloseClose: number[] = []; const rvParkinson: number[] = [];
    const rvGK: number[] = []; const rvYZ: number[] = [];
    const iv30: number[] = []; const ivVol: number[] = [];
    const skew: number[] = []; const termSlope: number[] = [];
    const dates: Date[] = []; const trendSlope: number[] = []; const atrPct: number[] = []; const volZ: number[] = [];
    const start = new Date(2025, 4, 1);

    for (let i = 0; i < N; i++) {
      const reg = regimePath[i];
      const trend = reg === 0 ? 0.0006 : reg === 2 ? 0.0010 : -0.0002;
      const sigma = reg === 0 ? 0.0070 : reg === 2 ? 0.0140 : 0.0190;
      const ret = trend + sigma * nrand(r);
      const open = price;
      const close = price * (1 + ret);
      const range = sigma * (1.5 + r());
      const high = Math.max(open, close) * (1 + range * r());
      const low  = Math.min(open, close) * (1 - range * r());
      const v = (8e6 + r() * 4e6) * (reg === 1 ? 1.6 : 1.0);
      candles.push({ o: open, h: high, l: low, c: close, v });

      const w = Math.min(30, i + 1);
      let cc = 0, pk = 0;
      for (let k = i - w + 1; k <= i; k++) {
        if (k <= 0) continue;
        const cd = candles[k]; const pd = candles[k - 1];
        const r1 = Math.log(cd.c / pd.c);
        cc += r1 * r1;
        const lh = Math.log(cd.h / cd.l);
        pk += lh * lh / (4 * Math.log(2));
      }
      cc = Math.sqrt(cc / w * 252);
      pk = Math.sqrt(pk / w * 252);
      const gk = pk * 0.94 + cc * 0.06;
      const yz = pk * 0.85 + cc * 0.15 + 0.001 * nrand(r);
      rvCloseClose.push(cc); rvParkinson.push(pk); rvGK.push(gk); rvYZ.push(yz);

      iv30.push(Math.max(0.05, yz + 0.04 + 0.018 * nrand(r)));
      ivVol.push(0.014 + 0.005 * Math.abs(nrand(r)));
      skew.push(-0.04 + 0.012 * nrand(r));
      termSlope.push(0.005 + 0.004 * nrand(r));

      trendSlope.push(reg === 0 ? 0.4 + 0.1 * nrand(r) : reg === 2 ? 0.6 + 0.1 * nrand(r) : -0.1 + 0.2 * nrand(r));
      atrPct.push(sigma * 100 * (1 + 0.1 * nrand(r)));
      volZ.push(reg === 1 ? 1.2 + 0.3 * nrand(r) : -0.2 + 0.3 * nrand(r));

      const d = new Date(start.getTime());
      d.setDate(start.getDate() + i + Math.floor(i / 5) * 2);
      dates.push(d);
      price = close;
    }

    const rvForward: number[] = [];
    for (let i = 0; i < N; i++) {
      if (i + 21 >= N) { rvForward.push(NaN); continue; }
      let s = 0;
      for (let k = i + 1; k <= i + 21; k++) {
        const r1 = Math.log(candles[k].c / candles[k - 1].c);
        s += r1 * r1;
      }
      rvForward.push(Math.sqrt(s / 21 * 252));
    }
    const vrpForward = iv30.map((iv, i) => Number.isNaN(rvForward[i]) ? NaN : iv - rvForward[i]);
    const valid = vrpForward.filter((x) => !Number.isNaN(x));
    const mean = valid.reduce((a, b) => a + b, 0) / valid.length;
    const std = Math.sqrt(valid.reduce((a, b) => a + (b - mean) ** 2, 0) / valid.length);
    const vrpZ = vrpForward.map((v) => Number.isNaN(v) ? NaN : (v - mean) / std);

    const vrpHistogram = histogram(valid, 20);

    const signals: SignalMark[] = [];
    const oracleSignals: SignalMark[] = [];
    for (let i = 0; i < N; i++) {
      if (Number.isNaN(vrpZ[i])) continue;
      if (vrpZ[i] > 1) signals.push({ i, dir: "short" });
      else if (vrpZ[i] < -1) signals.push({ i, dir: "long" });
    }
    for (let i = 0; i < N - 21; i++) {
      const fwdZ = (vrpForward[i] - mean) / std;
      if (fwdZ > 0.8 && i % 4 === 0) oracleSignals.push({ i, dir: "short" });
      else if (fwdZ < -0.8 && i % 5 === 0) oracleSignals.push({ i, dir: "long" });
    }

    const edgeComponents: EdgeComponents[] = [];
    const edgeScore: number[] = [];
    for (let i = 0; i < N; i++) {
      const z = Number.isNaN(vrpZ[i]) ? 0 : vrpZ[i];
      const vrpC = -Math.tanh(z * 0.6);
      const regimeC = regimePath[i] === 0 ? 0.0 : regimePath[i] === 1 ? 0.5 : -0.5;
      const ivC = -Math.tanh((iv30[i] - 0.18) * 4);
      const trendC = -Math.tanh(trendSlope[i] * 1.5);
      edgeComponents.push({ vrp: vrpC, regime: regimeC, iv: ivC, trend: trendC });
      edgeScore.push(vrpC * 0.4 + regimeC * 0.3 + ivC * 0.2 + trendC * 0.1);
    }

    const transitionMatrix = [
      [0.95, 0.015, 0.035],
      [0.05, 0.92, 0.03],
      [0.05, 0.02, 0.93],
    ];

    const assets = ["SPY", "QQQ", "IWM", "DIA"];
    const periods = ["2020", "2021", "2022", "2023", "2024", "2025-H1", "2025-H2", "2026-YTD"];
    const sharpeMatrix: number[][] = []; const heatmapStats: HeatmapStat[][] = [];
    for (let ai = 0; ai < assets.length; ai++) {
      const row: number[] = []; const stats: HeatmapStat[] = [];
      for (let pi = 0; pi < periods.length; pi++) {
        const baseSh = (ai === 0 ? 0.6 : ai === 1 ? 0.4 : ai === 2 ? -0.1 : 0.2) + nrand(r) * 0.6;
        row.push(baseSh);
        stats.push({
          sharpe: baseSh, n_trades: 18 + Math.floor(r() * 14),
          win_rate: 0.45 + r() * 0.15, max_dd: -(0.05 + r() * 0.10),
        });
      }
      sharpeMatrix.push(row); heatmapStats.push(stats);
    }

    const equityCurves: number[][] = assets.map((_, ai) => {
      const out: number[] = [100]; let v = 100;
      for (let i = 1; i < N; i++) {
        const ret = (ai === 0 ? 0.0008 : ai === 1 ? 0.0006 : ai === 2 ? -0.0002 : 0.0004) +
                    nrand(r) * 0.012;
        v = v * (1 + ret); out.push(v);
      }
      return out;
    });

    const perRegime: PerRegime[] = [
      { id: 0, name: "Trending · low vol", sharpe: 1.32, pnl: 184 },
      { id: 1, name: "Choppy · high vol", sharpe: -0.42, pnl: -64 },
      { id: 2, name: "Trending · high vol", sharpe: 0.81, pnl: 92 },
    ];

    const stability: number[] = [];
    for (let i = 0; i < 60; i++) {
      const v = 0.62 + 0.18 * Math.sin(i / 7) + 0.08 * nrand(r);
      stability.push(Math.max(0.2, Math.min(0.95, v)));
    }
    stability[42] = 0.41; stability[28] = 0.46;

    const trades: Trade[] = [];
    let equityT = 100; const equityFromTrades: number[] = [equityT];
    let totGross = 0, totSpread = 0, totSlip = 0, totComm = 0, totTradable = 0;
    for (let n = 1; n <= 18; n++) {
      const i = Math.min(N - 1, Math.floor(n * 14 + 6));
      const side: "LONG VOL" | "SHORT VOL" = n % 3 === 0 ? "SHORT VOL" : "LONG VOL";
      const entry = candles[i].c;
      const hold = 3 + Math.floor(r() * 5);
      const exit = candles[Math.min(N - 1, i + hold)].c;
      const dirSign = side === "LONG VOL" ? 1 : -1;
      const gross = (exit - entry) * dirSign * 1.4;
      const spread = -0.6 - r() * 0.4;
      const slip = -0.3 - r() * 0.25;
      const comm = -0.20;
      const tradable = r() > 0.18;
      trades.push({
        n, ts: dates[i].toISOString().slice(0, 10),
        side, entry, exit, hold,
        gross, spread, slip, comm, tradable,
      });
      const net = gross + spread + slip + comm;
      equityT += net * 0.4;
      equityFromTrades.push(equityT);
      totGross += gross; totSpread += spread; totSlip += slip; totComm += comm;
      if (tradable) totTradable += net;
    }

    const sparklines = {
      vrp: vrpZ.slice(-30).map((v) => Number.isNaN(v) ? 0 : v),
      equity: equityCurves[0].slice(-30),
      stability: stability.slice(-30),
    };

    return {
      N, dates, candles, iv30, ivVol, skew, termSlope,
      rvCloseClose, rvParkinson, rvGK, rvYZ, rvForward, vrpForward, vrpZ, vrpHistogram,
      regimePath, transitionMatrix, edgeScore, edgeComponents,
      signals, oracleSignals,
      coverage: { bars_total: N, forward_blind_tail: 21, iv_first_ts: dates[14].toISOString().slice(0, 10) },
      assets, periods, sharpeMatrix, heatmapStats, equityCurves,
      perRegime, stability,
      trades, equityFromTrades,
      costAttribution: {
        gross: totGross, spread: totSpread, slip: totSlip, comm: totComm,
        net: totGross + totSpread + totSlip + totComm,
        netTradable: totTradable,
      },
      sparklines,
    };
  }
}

function mulberry32(seed: number): () => number {
  return function (): number {
    seed |= 0; seed = (seed + 0x6D2B79F5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t ^= t + Math.imul(t ^ (t >>> 7), 61 | t);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function nrand(r: () => number): number {
  let u = 0, v = 0;
  while (u === 0) u = r();
  while (v === 0) v = r();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}

function histogram(values: number[], bins: number): VrpBin[] {
  const lo = Math.min(...values); const hi = Math.max(...values);
  const w = (hi - lo) / bins;
  const out: VrpBin[] = [];
  for (let i = 0; i < bins; i++) {
    const x0 = lo + i * w; const x1 = x0 + w;
    let count = 0;
    for (const v of values) if (v >= x0 && (v < x1 || i === bins - 1)) count++;
    out.push({ x0, x1, count });
  }
  return out;
}
