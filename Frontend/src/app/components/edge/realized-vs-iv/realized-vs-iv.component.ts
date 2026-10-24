import { ChangeDetectionStrategy, Component, computed, inject, signal } from "@angular/core";
import {
  EdgePriceIVChartComponent,
  EdgeVrpHistogramComponent,
  EDGE_TOK,
} from "../charts/edge-charts";
import { EdgeApiService, type Session } from "../services/edge-api.service";
import type { RealizedVsIvData } from "../services/edge-data.model";
import { SymbolPickerComponent } from "../../../shared/symbol-picker/symbol-picker.component";
import { TimestampDisplayPipe } from "../../../shared/timestamp";

@Component({
  selector: "app-edge-realized-vs-iv",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    EdgePriceIVChartComponent,
    EdgeVrpHistogramComponent,
    SymbolPickerComponent,
    TimestampDisplayPipe,
  ],
  templateUrl: "./realized-vs-iv.component.html",
  styleUrls: ["./realized-vs-iv.component.scss"],
})
export class RealizedVsIvComponent {
  private readonly api = inject(EdgeApiService);
  protected readonly TOK = EDGE_TOK;

  /** The last real Compute, or null. The page opens idle, and a failed Compute
   *  clears it: nothing is rendered that the endpoint did not return. */
  readonly data = signal<RealizedVsIvData | null>(null);
  readonly loading = signal(false);
  readonly errorMsg = signal<string | null>(null);

  readonly symbol = signal("SPY");
  readonly barSize = signal<"5m" | "15m" | "1h" | "1D">("1D");
  readonly tenor = signal<"7D" | "14D" | "30D" | "60D">("30D");

  /** ETH (04:00-20:00 ET) is the default. RTH (09:30-16:00) excludes
   *  pre-/post-market 15-min bars from the HF estimator. Daily-bar mode
   *  ignores this — the YZ-21 fallback is session-agnostic. */
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

  /** True iff the loaded series has no finite IV30 samples: the endpoint
   *  aligns recorded IV30 snapshots to the bar timestamps, and none landed. */
  readonly ivMissing = computed(() => {
    const d = this.data();
    return d !== null && !d.iv30.some((v) => Number.isFinite(v));
  });

  readonly liveIv30 = computed(() => this.data()?.liveIv30 ?? null);

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
    try {
      const result = await this.api.computeRealizedVsIv({
        symbol: this.symbol(),
        barSize: this.barSize(),
        tenor: this.tenor(),
        session: this.session(),
        estimators: ["yz"],
      });
      this.data.set(result);
      this.hoverIdx.set(null);
    } catch (e) {
      // An earlier result answers a different request; it must not stay on screen beside this error.
      this.data.set(null);
      this.errorMsg.set((e as Error).message);
    } finally {
      this.loading.set(false);
    }
  }

  protected setSession(s: Session): void { this.session.set(s); }
  protected sessionOn(s: Session): boolean { return this.session() === s; }

  protected fmtPct(v: number, digits = 1): string {
    if (Number.isNaN(v)) return "—";
    return (v * 100).toFixed(digits) + "%";
  }
}
