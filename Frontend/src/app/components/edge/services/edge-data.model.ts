import type { LakeStudyDataSource } from "../../../shared/lake-study/lake-study";

export interface Candle { o: number; h: number; l: number; c: number; v: number; }
export interface SignalMark { i: number; dir: "long" | "short"; }
export interface EdgeComponents { vrp: number; regime: number; iv: number; trend: number; }
export interface VrpBin { x0: number; x1: number; count: number; }
export interface HeatmapStat { sharpe: number; n_trades: number; win_rate: number; max_dd: number; }
export interface PerRegime { id: number; name: string; sharpe: number; pnl: number; }
export interface Trade {
  n: number; ts: number; side: "LONG VOL" | "SHORT VOL";
  entry: number; exit: number; hold: number;
  gross: number; spread: number; slip: number; comm: number; tradable: boolean;
}
export interface CostAttribution {
  gross: number; spread: number; slip: number; comm: number; net: number; netTradable: number;
}

export interface LiveIv30Marker {
  method: "vix_style" | "parametric";
  /** Solver-native ACT/365 IV30 value (decimal, e.g. 0.213 for 21.3%). */
  iv30Act365: number;
  snapshotTsMs: number;
  spot: number;
  /** Provenance signals piped through for the readout strip. */
  varianceContributionSynthetic: number;
  strikeCoverageScore: number;
}

/** The current-IV marker, kept apart from the IV series: it is a reading taken now, never a value on a past date. When
 *  neither live endpoint answered, the reason says why, rather than the marker silently going missing. */
export type LiveIv30State =
  | { readonly kind: "available"; readonly marker: LiveIv30Marker }
  | { readonly kind: "unavailable"; readonly reason: string };

export interface IvConfidenceSummary {
  ivSource: "caller_supplied" | "recorder" | "absent";
  latestConfidence: number | null;
  floor: number | null;
  gatedNow: boolean | null;
  /** Total bars in the response with floor_gated=true; 0 when the gating
   *  signal isn't present in the response. */
  nGated: number;
  /** True when the latest bar's `health_score` was imputed (the recorder
   *  row lacked it, so confidence dropped the health factor). Null when
   *  confidence wasn't computed for the run. Surface this so the UI can
   *  flag "confidence is based on imputed health" rather than presenting
   *  the number as fully evidence-backed. See ADR 0071 decision 11. */
  healthImputed: boolean | null;
}

/** The numbers the Edge hub headlines. They live in the dataset so they carry
 *  its provenance; no template holds a hardcoded figure. */
export interface EdgeHeadline {
  edgeComposite: number;
  robustness: number;
  dsrMean: number;
  pbo: number;
  /** Weighted per-component contribution to the composite. */
  contributions: EdgeComponents;
}

/** Generated demonstration data behind the Edge hub, Cross-asset, Regimes and
 *  the Edge score / Trade simulator drawers. Every number in it is synthetic,
 *  and `provenance` says so to anything that renders it. */
export interface EdgeSampleData {
  readonly provenance: "sample";
  N: number;
  dates: Date[];
  candles: Candle[];
  regimePath: number[];
  transitionMatrix: number[][];
  /** One row of normalized feature values per regime, in radar-axis order. */
  regimeCentroids: number[][];
  edgeScore: number[];
  edgeComponents: EdgeComponents[];
  headline: EdgeHeadline;
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

/** Trailing windows (bars) the study asks the endpoint for; it returns one
 *  series per estimator per window, keyed `<estimator>_<window>`. */
export const RV_WINDOWS: readonly number[] = [5, 10, 30];
/** The window the readout reports as `rv_yz_30_daily`. */
export const RV_READOUT_WINDOW = 30;

/** One trailing Yang-Zhang RV series the endpoint returned (`yz_<window>`). */
export interface RvWindowSeries {
  /** The trailing window, in bars. */
  window: number;
  values: number[];
}

/** Bar sizes the study is defined for. */
export type BarSize = "15m" | "1D";
/** The HF estimator's session filter; applies to 15-minute bars only. */
export type HfSession = "ETH" | "RTH";

/** The settings the run actually used, as the server echoed them: what the page's controls must show after a run. */
export interface RunSettings {
  barSize: BarSize;
  tenorDays: number;
  /** `null` for daily bars: there is no HF estimator, so no session was used. */
  hfSession: HfSession | null;
}

/** How recorded or supplied IV snapshots reached the bars (`app/engine/edge/iv_alignment.py`): each snapshot belongs to
 *  the bar that contains it, the latest wins, and nothing is carried or interpolated. */
export interface IvAlignment {
  rule: string;
  snapshotsRead: number;
  barsWithIv: number;
  /** Dropped because a later snapshot in the same bar was kept. */
  superseded: number;
  /** Inside no bar: a weekend, the hours between sessions, a bar with no trades. */
  outsideBars: number;
}

/** The IV the run was given. It is not from the lake: it is read from the IV recorder's file store, or supplied with the
 *  request, and the fingerprint names exactly the snapshots this run read. */
export interface IvInputSummary {
  source: "caller_supplied" | "recorder" | "absent";
  snapshots: number;
  /** sha256 of the points read; the identity of this run's IV input. */
  digest: string;
}

/** What the Realized vs IV study measured for one symbol. Only fields
 *  the study computes exist here: edge score, regimes, oracle signals and
 *  the trade simulator are not part of this study, so they are absent, never
 *  zero-filled. */
export interface RealizedVsIvData {
  readonly provenance: "real";
  symbol: string;
  N: number;
  dates: Date[];
  candles: Candle[];
  iv30: number[];
  /** IV30 in TRD/252 basis (post Step 1 conversion). Aligned for headline VRP. */
  iv30Trd252: number[];
  /** The trailing 30-bar Yang-Zhang series (`yz_30`), which the readout reports. */
  rvYZ: number[];
  /** Every trailing Yang-Zhang series the endpoint returned, shortest window first. */
  rvYzByWindow: RvWindowSeries[];
  /** RV used for VRP — HF two-component on 15m bars, falls back to YZ-21 on daily. TRD/252 basis. */
  rvHf21d: number[];
  vrpForward: number[];
  vrpZ: number[];
  vrpHistogram: VrpBin[];
  signals: SignalMark[];
  coverage: {
    forwardBlindTail: number;
    /** The bars of the VRP z-score's rolling window, as the server used them. The z-score exists only on a bar whose whole
     *  window has IV30 and a forward realized volatility. */
    vrpLookbackBars: number;
    ivFirstTsMs: number | null;
  };
  /** Populated only when the response carried IV provenance; null on the
   *  legacy "absent" path. */
  ivConfidence: IvConfidenceSummary | null;
  /** Where the IV came from and how many of its snapshots reached a bar. */
  ivAlignment: IvAlignment;
  /** The settings this result was computed with. */
  run: RunSettings;
  /** Where the bars came from: the lake, the window read, the counts. */
  dataSource: LakeStudyDataSource;
  /** Where the IV came from, which is not the lake. */
  ivInput: IvInputSummary;
  /** Live IV30 from /api/edge/iv30/{vix-style,parametric}, or why neither answered. */
  liveIv30: LiveIv30State;
}
