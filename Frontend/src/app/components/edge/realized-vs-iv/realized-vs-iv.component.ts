import { ChangeDetectionStrategy, Component, computed, inject, signal } from "@angular/core";
import { LakeStudySourceComponent } from "../../../shared/lake-study/lake-study-source.component";
import type { LakeStudyRefused } from "../../../shared/lake-study/lake-study";
import {
  EdgePriceIVChartComponent,
  EdgeVrpHistogramComponent,
  EDGE_TOK,
} from "../charts/edge-charts";
import { EdgeApiService, LakeStudyRefusedError, type Session, type Tenor } from "../services/edge-api.service";
import type { BarSize, RealizedVsIvData } from "../services/edge-data.model";
import { SymbolPickerComponent } from "../../../shared/symbol-picker/symbol-picker.component";
import { TimestampDisplayPipe } from "../../../shared/timestamp";

/** The bar sizes the study is defined for, and the tenors the basis conversion takes. Nothing here is offered that the
 *  backend would refuse or run as something else. */
const BAR_SIZES: readonly BarSize[] = ["15m", "1D"];
const TENORS: readonly Tenor[] = ["7D", "14D", "30D", "60D"];

@Component({
  selector: "app-edge-realized-vs-iv",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    EdgePriceIVChartComponent,
    EdgeVrpHistogramComponent,
    LakeStudySourceComponent,
    SymbolPickerComponent,
    TimestampDisplayPipe,
  ],
  templateUrl: "./realized-vs-iv.component.html",
  styleUrls: ["./realized-vs-iv.component.scss"],
})
export class RealizedVsIvComponent {
  private readonly api = inject(EdgeApiService);
  protected readonly TOK = EDGE_TOK;
  protected readonly barSizes = BAR_SIZES;
  protected readonly tenors = TENORS;
  protected readonly hfSessions: readonly Session[] = ["ETH", "RTH"];

  /** The last real Compute, or null. The page opens idle, and a failed Compute
   *  clears it: nothing is rendered that the endpoint did not return. */
  readonly data = signal<RealizedVsIvData | null>(null);
  readonly loading = signal(false);
  readonly errorMsg = signal<string | null>(null);
  /** The latest log line of the running study, so a long first run on a cold lake shows what it is doing. */
  readonly status = signal<string | null>(null);
  /** Set when the lake could not supply the bars: the reason and the counts admission reached. */
  readonly refused = signal<LakeStudyRefused | null>(null);

  readonly symbol = signal("SPY");
  readonly barSize = signal<BarSize>("1D");
  readonly tenor = signal<Tenor>("30D");

  /** ETH (04:00-20:00 ET) is the default. RTH (09:30-16:00) excludes
   *  pre-/post-market 15-min bars from the HF estimator. Daily bars have no
   *  HF estimator, so the control is offered for 15-minute bars only. */
  readonly session = signal<Session>("ETH");

  readonly hoverIdx = signal<number | null>(null);
  readonly currentIdx = computed(() => this.hoverIdx() ?? Math.floor((this.data()?.N ?? 0) / 2));

  readonly showRealtime = signal(true);
  readonly rvBands = signal(true);

  readonly blindStartMs = computed(() => {
    const d = this.data();
    if (!d) return null;
    return d.dates[Math.max(0, d.N - d.coverage.forwardBlindTail)]?.getTime() ?? null;
  });

  /** The name of the readout's trailing Yang-Zhang row: its bars are the run's bars, daily or 15-minute. */
  readonly rvReadoutKey = computed(() => `rv_yz_30_${this.data()?.run.barSize === "15m" ? "15m" : "daily"}`);

  readonly readout = computed(() => {
    const d = this.data();
    if (!d) return null;
    const i = this.currentIdx();
    const z = d.vrpZ[i];
    const action = Number.isNaN(z) ? "—"
      : z > 1 ? "SHORT VOL"
      : z < -1 ? "LONG VOL"
      : "NO TRADE";
    const color = action === "SHORT VOL" ? this.TOK.bear
      : action === "LONG VOL" ? this.TOK.bull : this.TOK.muted;
    return {
      i, z, action, color,
      iv30: d.iv30[i],
      iv30Trd252: d.iv30Trd252[i] ?? NaN,
      rvYzDaily: d.rvYZ[i],
      rvHf21d: d.rvHf21d[i] ?? NaN,
      vrpForward: d.vrpForward[i],
    };
  });

  readonly readoutHistogramN = computed(() =>
    this.data()?.vrpHistogram.reduce((acc, b) => acc + b.count, 0) ?? 0,
  );

  /** True iff the loaded series has no finite IV30 samples: no recorded or supplied snapshot fell inside a bar. */
  readonly ivMissing = computed(() => {
    const d = this.data();
    return d !== null && !d.iv30.some((v) => Number.isFinite(v));
  });

  /** Why there is no IV, from the counts the server reported: nothing recorded or supplied, or points none of which
   *  landed inside a bar. */
  readonly ivMissingReason = computed(() => {
    const d = this.data();
    if (d === null) return null;
    const read = d.ivAlignment.snapshotsRead;
    if (read === 0) return `The IV recorder holds no snapshot for ${d.symbol} in this window, and none was supplied.`;
    const plural = read === 1 ? "" : "s";
    return d.ivConfidence?.ivSource === "caller_supplied"
      ? `${read} supplied IV point${plural} ${read === 1 ? "was" : "were"} sent, but none falls inside a bar.`
      : `The IV recorder holds ${read} snapshot${plural} for ${d.symbol} in this window, but none falls inside a bar.`;
  });

  /** Why the page has no VRP z-score, hence no long-vol or short-vol markers and no `vrp_z` in the readout, when it has none
   *  anywhere. The z-score is a rolling statistic over `vrpLookbackBars` bars and needs a value on every one of them: a bar
   *  with a forward realized volatility (the last bars of a window have none yet) and IV30, which is never carried from
   *  one bar to the next. The reason is read off the window's own counts. */
  readonly signalUnavailableReason = computed<string | null>(() => {
    const d = this.data();
    if (d === null || d.vrpZ.some((z) => Number.isFinite(z))) return null;
    const need = d.coverage.vrpLookbackBars;
    const withForward = d.N - d.coverage.forwardBlindTail;
    if (withForward < need) {
      return `The z-score is measured over ${need} bars in a row that each have a forward realized volatility. This window has ${withForward} of ${d.N} (the last ${d.coverage.forwardBlindTail} are not realized yet), so it needs a longer window.`;
    }
    return `The z-score is measured over ${need} bars in a row that each have IV30, and IV30 is never carried from one bar to the next. IV30 is on ${d.ivAlignment.barsWithIv} of this window's ${d.N} bars, and no ${need} in a row have it.`;
  });

  /** The current-IV marker, when a live endpoint answered. It is a reading taken now and is never part of the IV series. */
  readonly liveMarker = computed(() => {
    const live = this.data()?.liveIv30;
    return live?.kind === "available" ? live.marker : null;
  });

  /** Why there is no current-IV marker, when neither live endpoint answered. */
  readonly liveUnavailableReason = computed(() => {
    const live = this.data()?.liveIv30;
    return live?.kind === "unavailable" ? live.reason : null;
  });

  readonly ivConfidence = computed(() => this.data()?.ivConfidence ?? null);

  /** Visual mode for the IV-confidence banner: "warn" when the latest bar
   *  is gated below the floor, "info" otherwise. Null hides the banner. */
  readonly ivConfidenceTone = computed<"warn" | "info" | null>(() => {
    const c = this.ivConfidence();
    if (!c) return null;
    return c.gatedNow === true ? "warn" : "info";
  });

  protected readonly ivSourceLabels: Record<"caller_supplied" | "recorder" | "absent", string> = {
    caller_supplied: "caller-supplied",
    recorder: "recorder fallback",
    absent: "no IV provided",
  };

  readonly rvLastValue = computed(() => {
    const arr = this.data()?.rvYZ ?? [];
    for (let i = arr.length - 1; i >= 0; i--) {
      if (Number.isFinite(arr[i])) return (arr[i] * 100).toFixed(1) + "%";
    }
    return "—";
  });

  protected async onCompute(): Promise<void> {
    this.loading.set(true);
    this.errorMsg.set(null);
    this.refused.set(null);
    this.status.set(null);
    try {
      const result = await this.api.computeRealizedVsIv({
        symbol: this.symbol(),
        barSize: this.barSize(),
        tenor: this.tenor(),
        hfSession: this.session(),
        estimators: ["yz"],
      }, (message) => this.status.set(message));
      this.data.set(result);
      this.hoverIdx.set(null);
    } catch (e) {
      // An earlier result answers a different request; it must not stay on screen beside this error.
      this.data.set(null);
      if (e instanceof LakeStudyRefusedError) this.refused.set(e.refused);
      else this.errorMsg.set(e instanceof Error ? e.message : String(e));
    } finally {
      this.loading.set(false);
      this.status.set(null);
    }
  }

  protected selectBarSize(event: Event): void {
    const value = event.target instanceof HTMLSelectElement ? event.target.value : "";
    const chosen = BAR_SIZES.find((b) => b === value);
    if (chosen !== undefined) this.barSize.set(chosen);
  }

  protected selectTenor(event: Event): void {
    const value = event.target instanceof HTMLSelectElement ? event.target.value : "";
    const chosen = TENORS.find((t) => t === value);
    if (chosen !== undefined) this.tenor.set(chosen);
  }

  protected setSession(s: Session): void { this.session.set(s); }
  protected sessionOn(s: Session): boolean { return this.session() === s; }

  protected fmtPct(v: number, digits = 1): string {
    if (Number.isNaN(v)) return "—";
    return (v * 100).toFixed(digits) + "%";
  }
}
