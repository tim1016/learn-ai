import { ChangeDetectionStrategy, Component, computed, inject, signal } from "@angular/core";
import { EdgeApiService, type Session } from "../services/edge-api.service";
import type { RealizedVsIvData } from "../services/edge-data.model";
import { SymbolPickerComponent } from "../../../shared/symbol-picker/symbol-picker.component";
import { createResultInputTracker } from "../../../shared/workspace/previous-result";
import { StudyPageComponent } from "../../../research/study-page/study-page.component";
import { RealizedVsIvReportComponent } from "./realized-vs-iv-report.component";

/** What a Compute is made from: the inputs a shown result answers. */
interface RealizedVsIvInputs {
  readonly symbol: string;
  readonly barSize: "5m" | "15m" | "1h" | "1D";
  readonly tenor: "7D" | "14D" | "30D" | "60D";
  readonly session: Session;
}

@Component({
  selector: "app-edge-realized-vs-iv",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    SymbolPickerComponent,
    StudyPageComponent,
    RealizedVsIvReportComponent,
  ],
  templateUrl: "./realized-vs-iv.component.html",
  styleUrls: ["./realized-vs-iv.component.scss"],
})
export class RealizedVsIvComponent {
  private readonly api = inject(EdgeApiService);

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

  /** The inputs the shown result answers; a result is "previous" once these change. */
  private readonly enteredInputs = computed<RealizedVsIvInputs>(() => ({
    symbol: this.symbol(),
    barSize: this.barSize(),
    tenor: this.tenor(),
    session: this.session(),
  }));
  readonly inputs = createResultInputTracker(this.enteredInputs, ({ symbol, barSize, tenor, session }) => ({
    key: JSON.stringify([symbol.toUpperCase(), barSize, tenor, session]),
    facts: [
      { label: "Instrument", value: symbol.toUpperCase() },
      { label: "Bar size", value: barSize },
      { label: "Tenor", value: tenor },
      { label: "HF session", value: session },
    ],
  }));

  /** What step 3 says is about to run. */
  readonly readySummary = computed(() => `${this.symbol().toUpperCase()}, ${this.barSize()} bars, ${this.tenor()} tenor`);

  /** Anything past the idle state is on screen: the request, an error or a result. */
  readonly hasOutput = computed(() => this.loading() || this.data() !== null || this.errorMsg() !== null);

  /**
   * Run study. `loading` closes synchronously, so a second press before the
   * request settles is dropped and Compute runs once.
   */
  protected async onCompute(): Promise<void> {
    if (this.loading()) return;
    this.loading.set(true);
    this.errorMsg.set(null);
    const submitted = this.enteredInputs();
    try {
      const result = await this.api.computeRealizedVsIv({
        symbol: submitted.symbol,
        barSize: submitted.barSize,
        tenor: submitted.tenor,
        session: submitted.session,
        estimators: ["yz"],
      });
      this.data.set(result);
      this.inputs.capture(submitted);
    } catch (e) {
      // An earlier result answers a different request; it must not stay on screen beside this error.
      this.data.set(null);
      this.inputs.clear();
      this.errorMsg.set((e as Error).message);
    } finally {
      this.loading.set(false);
    }
  }

  protected setSession(s: Session): void { this.session.set(s); }
  protected sessionOn(s: Session): boolean { return this.session() === s; }
}
