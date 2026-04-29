import { HttpClient } from "@angular/common/http";
import { inject, Injectable } from "@angular/core";
import { firstValueFrom } from "rxjs";
import type {
  Candle, EdgeData, IvConfidenceSummary, LiveIv30Marker, SignalMark, VrpBin,
} from "./edge-mock-data.service";

interface BarPayload {
  ts: number;
  open: number; high: number; low: number; close: number; volume: number;
}

interface AggregatesResponse {
  success: boolean;
  data: AggregateBarRaw[];
  summary: Record<string, unknown>;
  ticker: string;
  error?: string;
}

/** The python /api/aggregates/fetch sanitizer normalizes to long-form fields:
 *  open/high/low/close/volume + ISO `timestamp`. The Polygon SDK shorthand
 *  (t/o/h/l/c/v) is also supported below in case a different code path returns it. */
interface AggregateBarRaw {
  timestamp?: string | number;
  t?: number;
  open?: number; high?: number; low?: number; close?: number; volume?: number;
  o?: number;    h?: number;    l?: number;   c?: number;     v?: number;
}

interface RealizedVsIvSeriesResponse {
  symbol: string;
  ts: number[];
  rv_trailing: Record<string, (number | null)[]>;
  rv_forward: Record<string, (number | null)[]>;
  iv30: (number | null)[];
  iv30_trd252: (number | null)[];
  rv_hf_trailing: (number | null)[];
  rv_hf_forward: (number | null)[];
  vrp_forward: (number | null)[];
  vrp_z: (number | null)[];
  // IV-ownership Step E + recorder-fallback additions. Optional because old
  // server builds and the "absent" path may omit them.
  iv_source?: "caller_supplied" | "recorder" | "absent";
  confidence?: (number | null)[] | null;
  vrp_z_scaled?: (number | null)[] | null;
  floor_gated?: boolean[] | null;
  explanation?: {
    latest_confidence: number;
    floor: number;
    gated_now: boolean;
    /** Server-side flag (research-doc §7.11): the latest bar's confidence
     *  was computed via the drop-health-factor branch because the recorder
     *  row lacked an explicit health_score. Confidence collapsed to
     *  (1 − vcs); the UI should still mark the bar as "based on imputed
     *  evidence." Optional for backwards compat with server builds that
     *  pre-date the field. */
    health_imputed_now?: boolean;
  } | null;
  coverage: {
    n_bars: number;
    iv_first_ts: number | null;
    iv_last_ts: number | null;
    forward_nan_bars: number;
    session?: string;
    vrp_basis?: string;
    has_confidence?: boolean;
  };
}

interface Iv30LiveResponse {
  symbol: string;
  method: "vix_style" | "parametric";
  target_calendar_days: number;
  iv30_act365: number;
  spot: number;
  rate: number;
  dividend_yield: number;
  rate_source: string;
  dividend_source: string;
  expiries_used_calendar_days: number[];
  snapshot_ts_ms: number;
  iv_provenance: {
    iv_source: string;
    price_source_mix: Record<string, number>;
    variance_contribution_synthetic: number;
    strike_coverage_score: number;
  };
}

export type BarSize = "5m" | "15m" | "1h" | "1D";
export type Tenor = "7D" | "14D" | "30D" | "60D";
export type Estimator = "ctc" | "parkinson" | "gk" | "yz";
export type Session = "ETH" | "RTH";

export interface ComputeRvIvRequest {
  symbol: string;
  barSize: BarSize;
  tenor: Tenor;
  estimators: readonly Estimator[];
  session?: Session;
  windows?: readonly number[];
}

@Injectable({ providedIn: "root" })
export class EdgeApiService {
  private readonly http = inject(HttpClient);

  /** Fetch bars from /api/aggregates, post to /api/edge/realized-vs-iv/series,
   *  project the response into the EdgeData shape used by the chart components.
   *  Also fires a parallel live-IV30 fetch (vix-style → parametric fallback) and
   *  attaches the result so the chart can draw the live marker. */
  async computeRealizedVsIv(req: ComputeRvIvRequest): Promise<Partial<EdgeData>> {
    const bars = await this.fetchBars(req.symbol, req.barSize, req.tenor);
    if (bars.length === 0) {
      throw new Error(`no bars returned for ${req.symbol}`);
    }
    const tenorDays = this.tenorToDays(req.tenor);
    const seriesP = firstValueFrom(this.http.post<RealizedVsIvSeriesResponse>(
      "/api/edge/realized-vs-iv/series",
      {
        symbol: req.symbol,
        bar_size: req.barSize === "1D" ? "1d" : req.barSize === "15m" ? "15m" : "1d",
        tenor_days: tenorDays,
        session: req.session ?? "ETH",
        estimators: req.estimators,
        windows: req.windows ?? [5, 10, 30],
        bars,
      }
    ));
    const liveIvP = this.getLiveIv30(req.symbol, tenorDays);
    const series = await seriesP;
    const partial = this.projectIntoEdgeData(req.symbol, bars, series);
    partial.liveIv30 = await liveIvP;
    return partial;
  }

  /** Fetch the live IV30 value for `symbol`. Tries `vix-style` first
   *  (matches the server-side recorder preference); on failure falls back
   *  to `parametric`. Returns null when both endpoints error — the marker
   *  is non-load-bearing UI, so we don't surface the error to the page. */
  async getLiveIv30(symbol: string, targetCalendarDays = 30): Promise<LiveIv30Marker | null> {
    const tryOne = async (path: string, method: LiveIv30Marker["method"]) => {
      const resp = await firstValueFrom(this.http.post<Iv30LiveResponse>(
        path, { symbol, target_calendar_days: targetCalendarDays },
      ));
      return liveResponseToMarker(resp, method);
    };
    try { return await tryOne("/api/edge/iv30/vix-style", "vix_style"); }
    catch { /* fall through to parametric */ }
    try { return await tryOne("/api/edge/iv30/parametric", "parametric"); }
    catch { return null; }
  }

  private async fetchBars(symbol: string, barSize: BarSize, _tenor: Tenor): Promise<BarPayload[]> {
    const today = new Date();
    const from = new Date(today);
    // Reach back ~1 year for daily, ~30 days for intraday — enough warmup + a meaningful window.
    if (barSize === "1D") from.setFullYear(today.getFullYear() - 1);
    else from.setMonth(today.getMonth() - 1);

    const { multiplier, timespan } = this.barSizeToPolygon(barSize);
    const resp = await firstValueFrom(this.http.post<AggregatesResponse>(
      "/api/aggregates/fetch",
      {
        ticker: symbol,
        multiplier,
        timespan,
        from_date: from.toISOString().slice(0, 10),
        to_date: today.toISOString().slice(0, 10),
        limit: 50000,
        adjusted: true,
      }
    ));
    if (!resp.success || !resp.data) {
      throw new Error(resp.error ?? "aggregates fetch failed");
    }
    return resp.data
      .map((d) => this.normalizeBar(d))
      .filter((b): b is BarPayload => b !== null);
  }

  private normalizeBar(d: AggregateBarRaw): BarPayload | null {
    let ts: number;
    if (typeof d.timestamp === "number") ts = d.timestamp;
    else if (typeof d.timestamp === "string") ts = Date.parse(d.timestamp);
    else if (typeof d.t === "number") ts = d.t;
    else return null;
    if (!Number.isFinite(ts) || ts <= 0) return null;

    const open = d.open ?? d.o;
    const high = d.high ?? d.h;
    const low = d.low ?? d.l;
    const close = d.close ?? d.c;
    if (open == null || high == null || low == null || close == null) return null;

    return {
      ts, open, high, low, close,
      volume: d.volume ?? d.v ?? 0,
    };
  }

  private barSizeToPolygon(b: BarSize): { multiplier: number; timespan: string } {
    switch (b) {
      case "5m":  return { multiplier: 5,  timespan: "minute" };
      case "15m": return { multiplier: 15, timespan: "minute" };
      case "1h":  return { multiplier: 1,  timespan: "hour" };
      case "1D":  return { multiplier: 1,  timespan: "day" };
    }
  }

  private tenorToDays(t: Tenor): number {
    return Number(t.replace("D", ""));
  }

  /** Map the API series response into the chart-friendly EdgeData. Fields not
   *  returned by this endpoint (regimes, edge_components, etc.) get defaults so
   *  the existing chart components keep rendering. */
  private projectIntoEdgeData(
    symbol: string, bars: BarPayload[], series: RealizedVsIvSeriesResponse,
  ): Partial<EdgeData> {
    const N = bars.length;
    const candles: Candle[] = bars.map((b) => ({
      o: b.open, h: b.high, l: b.low, c: b.close, v: b.volume,
    }));
    const dates = bars.map((b) => new Date(b.ts));
    const iv30 = nullsToNaN(series.iv30);
    const iv30Trd252 = nullsToNaN(series.iv30_trd252 ?? []);
    const rvTrailingKey = Object.keys(series.rv_trailing)[0] ?? "yz_30";
    const rvForwardKey = Object.keys(series.rv_forward)[0] ?? "yz_30";
    const rvYZ = nullsToNaN(series.rv_trailing[rvTrailingKey] ?? []);
    const rvForward = nullsToNaN(series.rv_forward[rvForwardKey] ?? []);
    const rvHf21d = nullsToNaN(series.rv_hf_forward ?? []);
    const vrpForward = nullsToNaN(series.vrp_forward);
    const vrpZ = nullsToNaN(series.vrp_z);

    const validVrp = vrpForward.filter((x) => !Number.isNaN(x));
    const vrpHistogram: VrpBin[] = validVrp.length
      ? buildHistogram(validVrp, 20)
      : [];

    const signals: SignalMark[] = [];
    const oracleSignals: SignalMark[] = [];
    for (let i = 0; i < N; i++) {
      const z = vrpZ[i];
      if (Number.isNaN(z)) continue;
      if (z > 1) signals.push({ i, dir: "short" });
      else if (z < -1) signals.push({ i, dir: "long" });
    }

    return {
      N,
      dates,
      candles,
      iv30,
      ivVol: new Array(N).fill(NaN),
      skew: new Array(N).fill(NaN),
      termSlope: new Array(N).fill(NaN),
      rvCloseClose: new Array(N).fill(NaN),
      rvParkinson: new Array(N).fill(NaN),
      rvGK: new Array(N).fill(NaN),
      rvYZ,
      rvForward,
      rvHf21d,
      iv30Trd252,
      vrpForward,
      vrpZ,
      vrpHistogram,
      regimePath: new Array(N).fill(0),
      transitionMatrix: [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
      edgeScore: new Array(N).fill(0),
      edgeComponents: new Array(N).fill({ vrp: 0, regime: 0, iv: 0, trend: 0 }),
      signals,
      oracleSignals,
      coverage: {
        bars_total: series.coverage.n_bars,
        forward_blind_tail: series.coverage.forward_nan_bars,
        iv_first_ts: series.coverage.iv_first_ts ? new Date(series.coverage.iv_first_ts).toISOString().slice(0, 10) : "—",
      },
      ivConfidence: extractIvConfidence(series),
      sparklines: { vrp: [], equity: [], stability: [] },
    };
  }
}

function liveResponseToMarker(
  resp: Iv30LiveResponse,
  method: LiveIv30Marker["method"],
): LiveIv30Marker {
  return {
    method,
    iv30Act365: resp.iv30_act365,
    snapshotTsMs: resp.snapshot_ts_ms,
    spot: resp.spot,
    varianceContributionSynthetic: resp.iv_provenance.variance_contribution_synthetic,
    strikeCoverageScore: resp.iv_provenance.strike_coverage_score,
  };
}

function extractIvConfidence(
  series: RealizedVsIvSeriesResponse,
): IvConfidenceSummary | null {
  if (!series.iv_source) return null;
  const nGated = series.floor_gated
    ? series.floor_gated.reduce((acc, x) => acc + (x ? 1 : 0), 0)
    : 0;
  return {
    ivSource: series.iv_source,
    latestConfidence: series.explanation?.latest_confidence ?? null,
    floor: series.explanation?.floor ?? null,
    gatedNow: series.explanation?.gated_now ?? null,
    nGated,
    healthImputed: series.explanation?.health_imputed_now ?? null,
  };
}

function nullsToNaN(arr: readonly (number | null)[]): number[] {
  return arr.map((v) => (v === null || v === undefined ? NaN : v));
}

function buildHistogram(values: number[], bins: number): VrpBin[] {
  const lo = Math.min(...values); const hi = Math.max(...values);
  const w = (hi - lo) / bins; const out: VrpBin[] = [];
  for (let i = 0; i < bins; i++) {
    const x0 = lo + i * w; const x1 = x0 + w;
    let count = 0;
    for (const v of values) if (v >= x0 && (v < x1 || i === bins - 1)) count++;
    out.push({ x0, x1, count });
  }
  return out;
}
