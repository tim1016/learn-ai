import { HttpClient, HttpErrorResponse } from "@angular/common/http";
import { inject, Injectable } from "@angular/core";
import { firstValueFrom } from "rxjs";
import { JobsService } from "../../../services/jobs.service";
import {
  mapLakeStudyDataSource,
  readLakeStudyRefusal,
  type LakeDataSourceRaw,
  type LakeStudyRefused,
} from "../../../shared/lake-study/lake-study";
import {
  RV_READOUT_WINDOW, RV_WINDOWS,
  type BarSize, type Candle, type HfSession, type IvAlignment, type IvConfidenceSummary,
  type LiveIv30Marker, type LiveIv30State, type RealizedVsIvData, type RunSettings,
  type RvWindowSeries, type SignalMark, type VrpBin,
} from "./edge-data.model";

/** What the `realized_vs_iv` job parks as its result (`app/services/realized_vs_iv_study_service.py`): the series, the
 *  bars it was computed on, the settings it used, and where the bars came from. */
interface RealizedVsIvResultRaw {
  symbol: string;
  ts: number[];
  bars: { open: number[]; high: number[]; low: number[]; close: number[]; volume: number[] };
  rv_trailing: Record<string, (number | null)[]>;
  rv_forward: Record<string, (number | null)[]>;
  iv30: (number | null)[];
  iv30_trd252: (number | null)[];
  rv_hf_trailing: (number | null)[];
  rv_hf_forward: (number | null)[];
  vrp_forward: (number | null)[];
  vrp_z: (number | null)[];
  // IV-ownership Step E + recorder-fallback additions.
  iv_source: "caller_supplied" | "recorder" | "absent";
  confidence?: (number | null)[] | null;
  vrp_z_scaled?: (number | null)[] | null;
  floor_gated?: boolean[] | null;
  explanation?: {
    latest_confidence: number;
    floor: number;
    gated_now: boolean;
    /** Server-side flag (ADR 0071 decision 11): the latest bar's confidence
     *  was computed via the drop-health-factor branch because the recorder
     *  row lacked an explicit health_score. Confidence collapsed to
     *  (1 − vcs); the UI should still mark the bar as "based on imputed
     *  evidence." Optional for backwards compat with server builds that
     *  pre-date the field. */
    health_imputed_now?: boolean;
  } | null;
  bar_size: "15m" | "1d";
  tenor_days: number;
  hf_session: HfSession | null;
  coverage: {
    n_bars: number;
    iv_first_ts: number | null;
    iv_last_ts: number | null;
    forward_nan_bars: number;
    /** The bars of the VRP z-score's rolling window: it needs a value on every one of them. */
    vrp_lookback: number;
    session: string | null;
    vrp_basis?: string;
    has_confidence?: boolean;
    iv_alignment: {
      rule: string;
      snapshots_read: number;
      bars_with_iv: number;
      superseded: number;
      outside_bars: number;
    };
  };
  /** The lake block, with the fields this study adds to its identity: the IV it was given is named here. */
  data_source: LakeDataSourceRaw & {
    iv_source: "caller_supplied" | "recorder" | "absent";
    iv_snapshots: number;
    iv_digest: string;
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

export type { BarSize };
export type Tenor = "7D" | "14D" | "30D" | "60D";
export type Estimator = "ctc" | "parkinson" | "gk" | "yz";
export type Session = HfSession;

export interface ComputeRvIvRequest {
  symbol: string;
  barSize: BarSize;
  tenor: Tenor;
  estimators: readonly Estimator[];
  /** The HF estimator's session; only 15-minute bars have an HF estimator, so it is sent for them alone. */
  hfSession?: HfSession;
  windows?: readonly number[];
}

/** The lake could not supply the study's bars: the reason and the counts admission reached, for the data-source line. */
export class LakeStudyRefusedError extends Error {
  constructor(readonly refused: LakeStudyRefused) {
    super(refused.refusal.reason);
    this.name = "LakeStudyRefusedError";
  }
}

type JobEnd =
  | { kind: "completed" }
  | { kind: "failed"; message: string }
  | { kind: "cancelled"; reason: string };

/** The wire's bar size for the page's. The server is defined for these two and refuses any other with a reason. */
const WIRE_BAR_SIZE: Readonly<Record<BarSize, RealizedVsIvResultRaw["bar_size"]>> = { "15m": "15m", "1D": "1d" };

function barSizeFromWire(wire: RealizedVsIvResultRaw["bar_size"]): BarSize {
  return wire === "1d" ? "1D" : "15m";
}

@Injectable({ providedIn: "root" })
export class EdgeApiService {
  private readonly http = inject(HttpClient);
  private readonly jobs = inject(JobsService);

  /** Start the `realized_vs_iv` job (the worker reads the bars from the data lake, never the browser), wait for it, and
   *  project its result into the RealizedVsIvData shape used by the chart components. Also fires a parallel live-IV30
   *  fetch (vix-style → parametric fallback) and attaches the result, or why there is none, so the page can show the
   *  current-IV marker without it ever being placed on a past date.
   *
   *  A run the lake could not supply rejects with a {@link LakeStudyRefusedError} carrying the reason and the counts;
   *  any other failure rejects with its message. There is no fallback dataset. `onStatus` receives the job's log lines. */
  async computeRealizedVsIv(req: ComputeRvIvRequest, onStatus?: (message: string) => void): Promise<RealizedVsIvData> {
    const tenorDays = this.tenorToDays(req.tenor);
    const windows = req.windows ?? RV_WINDOWS;
    const wireBarSize = WIRE_BAR_SIZE[req.barSize];
    const jobId = await this.jobs.startJob("realized_vs_iv", {
      symbol: req.symbol,
      bar_size: wireBarSize,
      tenor_days: tenorDays,
      ...(wireBarSize === "15m" ? { hf_session: req.hfSession ?? "ETH" } : {}),
      estimators: req.estimators,
      windows,
    });
    const liveIvP = this.getLiveIv30(req.symbol, tenorDays);
    const end = await this.settle(jobId, onStatus);
    if (end.kind === "cancelled") throw new Error(end.reason);
    if (end.kind === "failed") {
      const refused = readLakeStudyRefusal(await this.parkedResult(jobId));
      if (refused !== null) throw new LakeStudyRefusedError(refused);
      throw new Error(end.message);
    }
    const result = await this.jobs.fetchResult<RealizedVsIvResultRaw>(jobId);
    return { ...this.projectResult(result, windows), liveIv30: await liveIvP };
  }

  /** Fetch the live IV30 value for `symbol`. Tries `vix-style` first
   *  (matches the server-side recorder preference); on failure falls back
   *  to `parametric`. When both fail the answer is `unavailable` with each
   *  endpoint's reason: the marker is non-load-bearing, but a missing one is
   *  never left to look like a market with no IV. */
  async getLiveIv30(symbol: string, targetCalendarDays = 30): Promise<LiveIv30State> {
    const attempts: readonly { path: string; method: LiveIv30Marker["method"]; label: string }[] = [
      { path: "/api/edge/iv30/vix-style", method: "vix_style", label: "vix-style" },
      { path: "/api/edge/iv30/parametric", method: "parametric", label: "parametric" },
    ];
    const failures: string[] = [];
    for (const { path, method, label } of attempts) {
      try {
        const resp = await firstValueFrom(this.http.post<Iv30LiveResponse>(
          path, { symbol, target_calendar_days: targetCalendarDays },
        ));
        return { kind: "available", marker: liveResponseToMarker(resp, method) };
      } catch (error) {
        failures.push(`${label}: ${describeFailure(error)}`);
      }
    }
    return { kind: "unavailable", reason: `Neither live IV30 endpoint answered. ${failures.join("; ")}.` };
  }

  /** Resolve when the job reaches a terminal state, passing its log lines on as it goes. `onEvent` replays what this
   *  tab already saw, so a job that finished before the listener attached still settles. */
  private settle(id: string, onStatus?: (message: string) => void): Promise<JobEnd> {
    return new Promise<JobEnd>((resolve) => {
      let unsubscribe: (() => void) | null = null;
      let settled = false;
      const finish = (end: JobEnd): void => {
        if (settled) return;
        settled = true;
        unsubscribe?.();
        resolve(end);
      };
      unsubscribe = this.jobs.onEvent(id, (event) => {
        switch (event.type) {
          case "job.log":
            if (typeof event["message"] === "string") onStatus?.(event["message"]);
            break;
          case "job.completed":
            finish({ kind: "completed" });
            break;
          case "job.failed":
            finish({ kind: "failed", message: typeof event["message"] === "string" ? event["message"] : "The run failed." });
            break;
          case "job.cancelled":
            finish({ kind: "cancelled", reason: typeof event["reason"] === "string" ? event["reason"] : "The run was cancelled." });
            break;
          default:
            break;
        }
      });
      // A terminal event replayed during the call above settled before `unsubscribe` was assigned.
      if (settled) unsubscribe();
    });
  }

  /** The result a failed job parked, or null when it parked none: only a refused window leaves one, so a 404 is the
   *  ordinary answer for any other failure. Anything else is a real error and is not swallowed. */
  private async parkedResult(id: string): Promise<unknown> {
    try {
      return await this.jobs.fetchResult<unknown>(id);
    } catch (error) {
      if (error instanceof HttpErrorResponse && error.status === 404) return null;
      throw error;
    }
  }

  private tenorToDays(t: Tenor): number {
    return Number(t.replace("D", ""));
  }

  /** Map the job result into the chart-friendly RealizedVsIvData.
   *  Only what the study returns is projected; nothing is defaulted for the
   *  edge score, regimes or oracle signals it does not compute. */
  private projectResult(
    result: RealizedVsIvResultRaw, windows: readonly number[],
  ): Omit<RealizedVsIvData, "liveIv30"> {
    const N = result.ts.length;
    const candles: Candle[] = result.ts.map((_, i) => ({
      o: result.bars.open[i], h: result.bars.high[i], l: result.bars.low[i], c: result.bars.close[i], v: result.bars.volume[i],
    }));
    const dates = result.ts.map((t) => new Date(t));
    const iv30 = nullsToNaN(result.iv30);
    const iv30Trd252 = nullsToNaN(result.iv30_trd252 ?? []);
    const rvYzByWindow = yangZhangTrailing(result, windows);
    const rvYZ = rvYzByWindow.find((s) => s.window === RV_READOUT_WINDOW)?.values;
    if (!rvYZ) {
      throw new Error(`the series response has no ${yzKey(RV_READOUT_WINDOW)} trailing RV for the readout`);
    }
    const rvHf21d = nullsToNaN(result.rv_hf_forward ?? []);
    const vrpForward = nullsToNaN(result.vrp_forward);
    const vrpZ = nullsToNaN(result.vrp_z);

    const validVrp = vrpForward.filter((x) => !Number.isNaN(x));
    const vrpHistogram: VrpBin[] = validVrp.length
      ? buildHistogram(validVrp, 20)
      : [];

    const signals: SignalMark[] = [];
    for (let i = 0; i < N; i++) {
      const z = vrpZ[i];
      if (Number.isNaN(z)) continue;
      if (z > 1) signals.push({ i, dir: "short" });
      else if (z < -1) signals.push({ i, dir: "long" });
    }

    const run: RunSettings = {
      barSize: barSizeFromWire(result.bar_size),
      tenorDays: result.tenor_days,
      hfSession: result.hf_session,
    };
    const alignment = result.coverage.iv_alignment;
    const ivAlignment: IvAlignment = {
      rule: alignment.rule,
      snapshotsRead: alignment.snapshots_read,
      barsWithIv: alignment.bars_with_iv,
      superseded: alignment.superseded,
      outsideBars: alignment.outside_bars,
    };

    return {
      provenance: "real",
      symbol: result.symbol,
      N,
      dates,
      candles,
      iv30,
      iv30Trd252,
      rvYZ,
      rvYzByWindow,
      rvHf21d,
      vrpForward,
      vrpZ,
      vrpHistogram,
      signals,
      coverage: {
        forwardBlindTail: result.coverage.forward_nan_bars,
        vrpLookbackBars: result.coverage.vrp_lookback,
        ivFirstTsMs: result.coverage.iv_first_ts,
      },
      ivConfidence: extractIvConfidence(result),
      ivAlignment,
      run,
      dataSource: mapLakeStudyDataSource(result.data_source),
      ivInput: {
        source: result.data_source.iv_source,
        snapshots: result.data_source.iv_snapshots,
        digest: result.data_source.iv_digest,
      },
    };
  }
}

const yzKey = (window: number): string => `yz_${window}`;

/** The Yang-Zhang series for the requested windows, picked by key: the
 *  response holds one series per estimator per window, so position says
 *  nothing about which estimator or window a series is. */
function yangZhangTrailing(
  series: RealizedVsIvResultRaw, windows: readonly number[],
): RvWindowSeries[] {
  return [...windows]
    .sort((a, b) => a - b)
    .flatMap((window) => {
      const values = series.rv_trailing[yzKey(window)];
      return values ? [{ window, values: nullsToNaN(values) }] : [];
    });
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
  series: RealizedVsIvResultRaw,
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

/** Why a live-IV request failed, in words: the server's own detail when it sent one, else the HTTP status. */
function describeFailure(error: unknown): string {
  if (error instanceof HttpErrorResponse) {
    if (error.status === 0) return "the data service did not answer";
    const detail: unknown = (error.error as { detail?: unknown } | null)?.detail;
    return typeof detail === "string" && detail !== "" ? `${error.status} ${detail}` : `${error.status} ${error.statusText}`;
  }
  return error instanceof Error ? error.message : "an unexpected error";
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
