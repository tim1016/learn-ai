import { ChangeDetectionStrategy, Component, computed, inject, input, signal } from "@angular/core";
import { EdgeApiService, LakeStudyRefusedError, type Session, type Tenor } from "../services/edge-api.service";
import type { BarSize, RealizedVsIvData } from "../services/edge-data.model";
import { SymbolPickerComponent } from "../../../shared/symbol-picker/symbol-picker.component";
import { createResultInputTracker } from "../../../shared/workspace/previous-result";
import type { DataSourceProvenance } from "../../../shared/workspace/data-source-provenance";
import { LakeStudySourceComponent } from "../../../shared/lake-study/lake-study-source.component";
import { toDataSourceProvenance, type LakeStudyRefused } from "../../../shared/lake-study/lake-study";
import { StudyPageComponent } from "../../../research/study-page/study-page.component";
import { createRerunPrefill } from "../../../research/stored-study/rerun";
import { StoredInputsError, type StoredInputs } from "../../../research/stored-study/stored-inputs";
import { RealizedVsIvReportComponent } from "./realized-vs-iv-report.component";

/** The bar sizes the study is defined for, and the tenors the basis conversion takes. Nothing here is offered that the
 *  backend would refuse or run as something else. */
const BAR_SIZES: readonly BarSize[] = ["15m", "1D"];
const TENORS: readonly Tenor[] = ["7D", "14D", "30D", "60D"];

/** What a Compute is made from: the inputs a shown result answers. */
interface RealizedVsIvInputs {
  readonly symbol: string;
  readonly barSize: BarSize;
  readonly tenor: Tenor;
  /** The HF estimator's session; only 15-minute bars have one. */
  readonly session: Session;
}

@Component({
  selector: "app-edge-realized-vs-iv",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    LakeStudySourceComponent,
    SymbolPickerComponent,
    StudyPageComponent,
    RealizedVsIvReportComponent,
  ],
  templateUrl: "./realized-vs-iv.component.html",
  styleUrls: ["./realized-vs-iv.component.scss"],
})
export class RealizedVsIvComponent {
  private readonly api = inject(EdgeApiService);
  protected readonly barSizes = BAR_SIZES;
  protected readonly tenors = TENORS;
  protected readonly hfSessions: readonly Session[] = ["ETH", "RTH"];

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

  /** The inputs the shown result answers; a result is "previous" once these change. */
  private readonly enteredInputs = computed<RealizedVsIvInputs>(() => ({
    symbol: this.symbol(),
    barSize: this.barSize(),
    tenor: this.tenor(),
    session: this.session(),
  }));
  readonly inputs = createResultInputTracker(this.enteredInputs, ({ symbol, barSize, tenor, session }) => ({
    key: JSON.stringify([symbol.toUpperCase(), barSize, tenor, barSize === "15m" ? session : null]),
    facts: [
      { label: "Instrument", value: symbol.toUpperCase() },
      { label: "Bar size", value: barSize },
      { label: "Tenor", value: tenor },
      ...(barSize === "15m" ? [{ label: "HF session", value: session }] : []),
    ],
  }));

  /**
   * The data-source line for what is on screen: the shown result's lake source, or the source a refused run was refused
   * against. `undefined` before either exists, which leaves the study's own pre-run line.
   */
  readonly sourceLine = computed<DataSourceProvenance | undefined>(() => {
    const source = this.data()?.dataSource ?? this.refused()?.dataSource ?? null;
    return source === null ? undefined : toDataSourceProvenance(source);
  });

  /** What step 3 says is about to run. */
  readonly readySummary = computed(() => `${this.symbol().toUpperCase()}, ${this.barSize()} bars, ${this.tenor()} tenor`);

  /** Anything past the idle state is on screen: the request, an error, a refusal or a result. */
  readonly hasOutput = computed(
    () => this.loading() || this.data() !== null || this.errorMsg() !== null || this.refused() !== null,
  );

  /**
   * Run study. `loading` closes synchronously, so a second press before the
   * request settles is dropped and Compute runs once.
   */
  protected async onCompute(): Promise<void> {
    if (this.loading()) return;
    this.loading.set(true);
    this.errorMsg.set(null);
    this.refused.set(null);
    this.status.set(null);
    const submitted = this.enteredInputs();
    try {
      const parent = this.prefill.parent();
      const result = await this.api.computeRealizedVsIv(
        {
          symbol: submitted.symbol,
          barSize: submitted.barSize,
          tenor: submitted.tenor,
          hfSession: submitted.session,
          estimators: ["yz"],
          ...(parent === null ? {} : { rerunOf: parent }),
        },
        (message) => this.status.set(message),
      );
      this.prefill.started();
      this.data.set(result);
      this.inputs.capture(submitted);
    } catch (e) {
      // An earlier result answers a different request; it must not stay on screen beside this error.
      this.data.set(null);
      this.inputs.clear();
      if (e instanceof LakeStudyRefusedError) this.refused.set(e.refused);
      else this.errorMsg.set(e instanceof Error ? e.message : String(e));
    } finally {
      this.loading.set(false);
      this.status.set(null);
    }
  }

  /**
   * Sets the inputs from a saved run's. The run recorded the bar size the study was told ("15m" or "1d"); a run asked
   * for any other size was refused and fills in nothing. Everything is read first, so a field it cannot read leaves the
   * page as it was.
   */
  private fillFrom(stored: StoredInputs): void {
    const sent = stored.text("bar_size");
    const barSize = sent === "15m" ? "15m" : sent === "1d" ? "1D" : null;
    if (barSize === null) throw new StoredInputsError(`"${sent}" is not a bar size this page offers`);
    const days = `${stored.number("tenor_days")}D`;
    const tenor = TENORS.find((offered) => offered === days);
    if (tenor === undefined) throw new StoredInputsError(`a ${days} tenor is not offered here`);
    const session = stored.text("hf_session");
    if (session !== "ETH" && session !== "RTH") throw new StoredInputsError(`"${session}" is not a session this page offers`);
    this.symbol.set(stored.text("symbol").toUpperCase());
    this.barSize.set(barSize);
    this.tenor.set(tenor);
    this.session.set(session);
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
}
