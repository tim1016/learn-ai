import { ChangeDetectionStrategy, Component, computed, inject, input, signal } from "@angular/core";
import { EdgeApiService, type Session } from "../services/edge-api.service";
import type { RealizedVsIvData } from "../services/edge-data.model";
import { SymbolPickerComponent } from "../../../shared/symbol-picker/symbol-picker.component";
import { createResultInputTracker } from "../../../shared/workspace/previous-result";
import { StudyPageComponent } from "../../../research/study-page/study-page.component";
import { createRerunPrefill } from "../../../research/stored-study/rerun";
import { StoredInputsError, type StoredInputs } from "../../../research/stored-study/stored-inputs";
import { RealizedVsIvReportComponent } from "./realized-vs-iv-report.component";

/** The tenors the page offers. */
const TENORS: readonly string[] = ["7D", "14D", "30D", "60D"];

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

  /** `?rerun=<saved run id>`: fill the inputs from that run. It runs nothing; Run study is still the reader's. */
  readonly rerun = input<string>();
  private readonly prefill = createRerunPrefill({
    study: "realized-vs-iv",
    id: this.rerun,
    apply: (stored) => this.fillFrom(stored),
  });
  readonly prefillNotice = this.prefill.notice;

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
      const parent = this.prefill.parent();
      const result = await this.api.computeRealizedVsIv({
        symbol: submitted.symbol,
        barSize: submitted.barSize,
        tenor: submitted.tenor,
        session: submitted.session,
        estimators: ["yz"],
        ...(parent === null ? {} : { rerunOf: parent }),
      });
      this.prefill.started();
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

  /**
   * Sets the inputs from a saved run's. The run recorded the bar size the study was told ("15m" or "1d"), so a run
   * that was asked for 5m or 1h bars, which the study treats as daily, fills in as 1D. Everything is read first, so a
   * field it cannot read leaves the page as it was.
   */
  private fillFrom(stored: StoredInputs): void {
    const sent = stored.text("bar_size");
    const barSize = sent === "15m" ? "15m" : sent === "1d" ? "1D" : null;
    if (barSize === null) throw new StoredInputsError(`"${sent}" is not a bar size this page offers`);
    const tenor = `${stored.number("tenor_days")}D`;
    if (!TENORS.includes(tenor)) throw new StoredInputsError(`a ${tenor} tenor is not offered here`);
    const session = stored.text("session");
    if (session !== "ETH" && session !== "RTH") throw new StoredInputsError(`"${session}" is not a session this page offers`);
    this.symbol.set(stored.text("symbol").toUpperCase());
    this.barSize.set(barSize);
    this.tenor.set(tenor as RealizedVsIvInputs["tenor"]);
    this.session.set(session);
  }

  protected setSession(s: Session): void { this.session.set(s); }
  protected sessionOn(s: Session): boolean { return this.session() === s; }
}
