import {
  RV_READOUT_WINDOW,
  type Candle, type IvConfidenceSummary, type RealizedVsIvData,
  type RvWindowSeries, type SignalMark, type VrpBin,
} from "./edge-data.model";

export interface BarPayload {
  ts: number;
  open: number; high: number; low: number; close: number; volume: number;
}

export interface RealizedVsIvSeriesResponse {
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
    /** Server-side flag (ADR 0071 decision 11): the latest bar's confidence
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

/** Map the API series response into the chart-friendly RealizedVsIvData.
 *  Only what the endpoint returns is projected; nothing is defaulted for the
 *  edge score, regimes or oracle signals it does not compute. */
export function projectRealizedVsIvSeries(
  symbol: string, bars: readonly BarPayload[], series: RealizedVsIvSeriesResponse, windows: readonly number[],
): Omit<RealizedVsIvData, "liveIv30"> {
  const N = bars.length;
  const candles: Candle[] = bars.map((b) => ({
    o: b.open, h: b.high, l: b.low, c: b.close, v: b.volume,
  }));
  const dates = bars.map((b) => new Date(b.ts));
  const iv30 = nullsToNaN(series.iv30);
  const iv30Trd252 = nullsToNaN(series.iv30_trd252 ?? []);
  const rvYzByWindow = yangZhangTrailing(series, windows);
  const rvYZ = rvYzByWindow.find((s) => s.window === RV_READOUT_WINDOW)?.values;
  if (!rvYZ) {
    throw new Error(`the series response has no ${yzKey(RV_READOUT_WINDOW)} trailing RV for the readout`);
  }
  const rvHf21d = nullsToNaN(series.rv_hf_forward ?? []);
  const vrpForward = nullsToNaN(series.vrp_forward);
  const vrpZ = nullsToNaN(series.vrp_z);

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

  return {
    provenance: "real",
    symbol,
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
      forwardBlindTail: series.coverage.forward_nan_bars,
      ivFirstTsMs: series.coverage.iv_first_ts,
    },
    ivConfidence: extractIvConfidence(series),
  };
}

const yzKey = (window: number): string => `yz_${window}`;

/** The Yang-Zhang series for the requested windows, picked by key: the
 *  response holds one series per estimator per window, so position says
 *  nothing about which estimator or window a series is. */
function yangZhangTrailing(
  series: RealizedVsIvSeriesResponse, windows: readonly number[],
): RvWindowSeries[] {
  return [...windows]
    .sort((a, b) => a - b)
    .flatMap((window) => {
      const values = series.rv_trailing[yzKey(window)];
      return values ? [{ window, values: nullsToNaN(values) }] : [];
    });
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
