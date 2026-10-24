import { HttpClient } from "@angular/common/http";
import { inject, Injectable } from "@angular/core";
import { firstValueFrom } from "rxjs";
import { RV_WINDOWS, type LiveIv30Marker, type RealizedVsIvData } from "./edge-data.model";
import {
  projectRealizedVsIvSeries,
  type BarPayload, type RealizedVsIvSeriesResponse,
} from "./realized-vs-iv-projection";

interface AggregatesResponse {
  success: boolean;
  data: AggregateBarRaw[];
  summary: Record<string, unknown>;
  ticker: string;
  error?: string;
}

/** The python /api/aggregates/fetch sanitizer normalizes to long-form fields:
 *  open/high/low/close/volume + int64-ms `timestamp`. The Polygon SDK shorthand
 *  (t/o/h/l/c/v) is also supported below in case a different code path returns it. */
interface AggregateBarRaw {
  timestamp?: number;
  t?: number;
  open?: number; high?: number; low?: number; close?: number; volume?: number;
  o?: number;    h?: number;    l?: number;   c?: number;     v?: number;
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
   *  project the response into the RealizedVsIvData shape used by the chart
   *  components. Also fires a parallel live-IV30 fetch (vix-style → parametric
   *  fallback) and attaches the result so the chart can draw the live marker.
   *  A failed request rejects; there is no fallback dataset. */
  async computeRealizedVsIv(req: ComputeRvIvRequest): Promise<RealizedVsIvData> {
    const bars = await this.fetchBars(req.symbol, req.barSize, req.tenor);
    if (bars.length === 0) {
      throw new Error(`no bars returned for ${req.symbol}`);
    }
    const tenorDays = this.tenorToDays(req.tenor);
    const windows = req.windows ?? RV_WINDOWS;
    const seriesP = firstValueFrom(this.http.post<RealizedVsIvSeriesResponse>(
      "/api/edge/realized-vs-iv/series",
      {
        symbol: req.symbol,
        bar_size: req.barSize === "1D" ? "1d" : req.barSize === "15m" ? "15m" : "1d",
        tenor_days: tenorDays,
        session: req.session ?? "ETH",
        estimators: req.estimators,
        windows,
        bars,
      }
    ));
    const liveIvP = this.getLiveIv30(req.symbol, tenorDays);
    const series = await seriesP;
    return { ...projectRealizedVsIvSeries(req.symbol, bars, series, windows), liveIv30: await liveIvP };
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
