import { mapLakeStudyDataSource, type LakeDataSourceRaw } from "../../../shared/lake-study/lake-study";
import {
  RV_READOUT_WINDOW,
  type BarSize, type Candle, type HfSession, type IvAlignment, type IvConfidenceSummary,
  type RealizedVsIvData, type RunSettings, type RvWindowSeries, type SignalMark, type VrpBin,
} from "./edge-data.model";

/** What the `realized_vs_iv` job parks as its result (`app/services/realized_vs_iv_study_service.py`): the series, the
 *  bars it was computed on, the settings it used, and where the bars came from. */
export interface RealizedVsIvResultRaw {
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

/** The wire's bar size for the page's. The server is defined for these two and refuses any other with a reason. */
export const WIRE_BAR_SIZE: Readonly<Record<BarSize, RealizedVsIvResultRaw["bar_size"]>> = { "15m": "15m", "1D": "1d" };

function barSizeFromWire(wire: RealizedVsIvResultRaw["bar_size"]): BarSize {
  return wire === "1d" ? "1D" : "15m";
}

/** Map a Realized vs IV result, as the job delivers it or a saved run holds it, into the chart-friendly RealizedVsIvData.
 *  Only what the study returns is projected; nothing is defaulted for the
 *  edge score, regimes or oracle signals it does not compute. */
export function projectRealizedVsIvResult(
  result: RealizedVsIvResultRaw, windows: readonly number[],
): Omit<RealizedVsIvData, "liveIv30"> {
  // A saved run from before the study read the lake held the bars the caller posted, as a list, and no data source.
  const bars: unknown = result.bars;
  if (typeof bars !== "object" || bars === null || Array.isArray(bars) || !("close" in bars) || result.data_source === undefined) {
    throw new Error("this run was saved in a form from before the study read the data lake, which this page can no longer draw");
  }
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
