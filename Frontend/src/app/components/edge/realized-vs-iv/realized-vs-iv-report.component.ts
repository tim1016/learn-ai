import { ChangeDetectionStrategy, Component, computed, input, linkedSignal, signal } from "@angular/core";
import {
  EdgePriceIVChartComponent,
  EdgeVrpHistogramComponent,
  EDGE_TOK,
} from "../charts/edge-charts";
import type { RealizedVsIvData } from "../services/edge-data.model";
import { TimestampDisplayPipe } from "../../../shared/timestamp";

/**
 * What one Realized-vs-IV Compute measured: coverage and IV-confidence
 * banners, the price/IV chart, the VRP histogram and the signal readout.
 * It draws from `data` alone, so a stored result renders the way a fresh one
 * does.
 */
@Component({
  selector: "app-realized-vs-iv-report",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [EdgePriceIVChartComponent, EdgeVrpHistogramComponent, TimestampDisplayPipe],
  templateUrl: "./realized-vs-iv-report.component.html",
  styleUrls: ["./realized-vs-iv-report.component.scss"],
})
export class RealizedVsIvReportComponent {
  protected readonly TOK = EDGE_TOK;

  readonly data = input.required<RealizedVsIvData>();

  /** The bar under the cursor; a new result starts with none. */
  readonly hoverIdx = linkedSignal<RealizedVsIvData, number | null>({
    source: this.data,
    computation: () => null,
  });
  readonly currentIdx = computed(() => this.hoverIdx() ?? Math.floor(this.data().N / 2));

  readonly showRealtime = signal(true);
  readonly rvBands = signal(true);

  readonly blindStartMs = computed(() => {
    const d = this.data();
    return d.dates[Math.max(0, d.N - d.coverage.forwardBlindTail)]?.getTime() ?? null;
  });

  readonly readout = computed(() => {
    const d = this.data();
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
    this.data().vrpHistogram.reduce((acc, b) => acc + b.count, 0),
  );

  /** The name of the readout's trailing Yang-Zhang row: its bars are the run's bars, daily or 15-minute. */
  readonly rvReadoutKey = computed(() => `rv_yz_30_${this.data().run.barSize === "15m" ? "15m" : "daily"}`);

  /** True iff the loaded series has no finite IV30 samples: no recorded or supplied snapshot fell inside a bar. */
  readonly ivMissing = computed(() => !this.data().iv30.some((v) => Number.isFinite(v)));

  /** Why there is no IV, from the counts the server reported: nothing recorded or supplied, or points none of which
   *  landed inside a bar. */
  readonly ivMissingReason = computed(() => {
    const d = this.data();
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
    if (d.vrpZ.some((z) => Number.isFinite(z))) return null;
    const need = d.coverage.vrpLookbackBars;
    const withForward = d.N - d.coverage.forwardBlindTail;
    if (withForward < need) {
      return `The z-score is measured over ${need} bars in a row that each have a forward realized volatility. This window has ${withForward} of ${d.N} (the last ${d.coverage.forwardBlindTail} are not realized yet), so it needs a longer window.`;
    }
    return `The z-score is measured over ${need} bars in a row that each have IV30, and IV30 is never carried from one bar to the next. IV30 is on ${d.ivAlignment.barsWithIv} of this window's ${d.N} bars, and no ${need} in a row have it.`;
  });

  /** The current-IV marker, when a live endpoint answered. It is a reading taken now and is never part of the IV series. */
  readonly liveMarker = computed(() => {
    const live = this.data().liveIv30;
    return live.kind === "available" ? live.marker : null;
  });

  /** Why there is no current-IV marker, when neither live endpoint answered (and why a saved run has none). */
  readonly liveUnavailableReason = computed(() => {
    const live = this.data().liveIv30;
    return live.kind === "unavailable" ? live.reason : null;
  });

  readonly ivConfidence = computed(() => this.data().ivConfidence);

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
    const arr = this.data().rvYZ;
    for (let i = arr.length - 1; i >= 0; i--) {
      if (Number.isFinite(arr[i])) return (arr[i] * 100).toFixed(1) + "%";
    }
    return "—";
  });

  protected fmtPct(v: number, digits = 1): string {
    if (Number.isNaN(v)) return "—";
    return (v * 100).toFixed(digits) + "%";
  }
}
