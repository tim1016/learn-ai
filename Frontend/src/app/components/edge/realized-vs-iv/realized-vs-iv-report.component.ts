import { ChangeDetectionStrategy, Component, computed, input, linkedSignal, signal } from "@angular/core";
import {
  EdgePriceIVChartComponent,
  EdgeVrpHistogramComponent,
  EDGE_TOK,
} from "../charts/edge-charts";
import type { BarSize } from "../services/edge-api.service";
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
  /** The bars `data` was computed on; the chart's date axis reads daily and intraday bars differently. */
  readonly barSize = input<BarSize>("1D");

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

  /** True iff the loaded series has no finite IV30 samples: the endpoint
   *  aligns recorded IV30 snapshots to the bar timestamps, and none landed. */
  readonly ivMissing = computed(() => !this.data().iv30.some((v) => Number.isFinite(v)));

  /** Null for a stored result, which never carries the live marker. */
  readonly liveIv30 = computed(() => this.data().liveIv30);

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
