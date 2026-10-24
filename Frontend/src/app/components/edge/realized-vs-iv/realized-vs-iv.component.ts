import { ChangeDetectionStrategy, Component, computed, inject, input, resource, signal } from "@angular/core";
import { describeFailure, EdgeApiService, LakeStudyRefusedError, type DefaultWindow, type Tenor } from "../services/edge-api.service";
import type { BarSize, RealizedVsIvData } from "../services/edge-data.model";
import { SymbolPickerComponent } from "../../../shared/symbol-picker/symbol-picker.component";
import { createResultInputTracker } from "../../../shared/workspace/previous-result";
import type { DataSourceProvenance } from "../../../shared/workspace/data-source-provenance";
import { LakeStudySourceComponent } from "../../../shared/lake-study/lake-study-source.component";
import { LakeHoldingNoticeComponent } from "../../../shared/lake-study/lake-holding-notice.component";
import { createLakeHolding } from "../../../shared/lake-study/create-lake-holding";
import {
  LAKE_STUDY_TREE,
  SESSION_POLICY_FOR_REQUEST,
  sessionPolicyWords,
  toDataSourceProvenance,
  type LakeStudyRefused,
} from "../../../shared/lake-study/lake-study";
import type { InstrumentDataRequest } from "../../../shared/data-lake";
import type { Session } from "../../../shared/ticker-range-picker/ticker-range-picker.types";
import { etIsoDate } from "../../../shared/date/et-midnight";
import { TimestampDisplayComponent, formatTimestampDisplay } from "../../../shared/timestamp";
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
  /** The session the bars are folded from: the page's choice for 15-minute bars, regular hours for daily bars. */
  readonly session: Session;
  /** The window the run reads, as the server resolved it; `null` while it is unread, and the server then resolves it. */
  readonly window: { readonly startMs: number; readonly endMs: number } | null;
}

@Component({
  selector: "app-edge-realized-vs-iv",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    LakeStudySourceComponent,
    LakeHoldingNoticeComponent,
    SymbolPickerComponent,
    StudyPageComponent,
    RealizedVsIvReportComponent,
    TimestampDisplayComponent,
  ],
  templateUrl: "./realized-vs-iv.component.html",
  styleUrls: ["./realized-vs-iv.component.scss"],
})
export class RealizedVsIvComponent {
  private readonly api = inject(EdgeApiService);
  protected readonly barSizes = BAR_SIZES;
  protected readonly tenors = TENORS;
  protected readonly sessions: readonly Session[] = ["rth", "extended"];

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

  /**
   * The session a 15-minute run folds its bars from: extended hours (04:00-20:00 ET, what the study has always read) or
   * regular hours (09:30-16:00). The HF estimator follows it. Daily bars read regular hours only, so the control is
   * offered for 15-minute bars alone (#3525).
   */
  readonly session = signal<Session>("extended");

  /** The session the run reads: the page's choice for 15-minute bars, regular hours for daily bars. */
  readonly runSession = computed<Session>(() => (this.barSize() === "1D" ? "rth" : this.session()));

  private readonly windowRead = resource({
    params: () => ({ barSize: this.barSize(), session: this.runSession() }),
    loader: ({ params }) => this.api.getDefaultWindow(params.barSize, params.session),
  });

  /** The window a run would read now, as the server resolves it for the bar size and session on screen; `null` until read. */
  readonly defaultWindow = computed<DefaultWindow | null>(() => {
    if (!this.windowRead.hasValue()) return null;
    const read = this.windowRead.value();
    return read.barSize === this.barSize() && read.session === this.runSession() ? read : null;
  });
  protected readonly windowFailure = computed(() => {
    const error = this.windowRead.error();
    return error === undefined ? null : describeFailure(error);
  });
  protected readonly sessionWords = computed(() => sessionPolicyWords(SESSION_POLICY_FOR_REQUEST[this.runSession()]));

  /** The inputs the shown result answers; a result is "previous" once these change. */
  private readonly enteredInputs = computed<RealizedVsIvInputs>(() => {
    const window = this.defaultWindow();
    return {
      symbol: this.symbol(),
      barSize: this.barSize(),
      tenor: this.tenor(),
      session: this.runSession(),
      window: window === null ? null : { startMs: window.windowStartMs, endMs: window.windowEndMs },
    };
  });
  readonly inputs = createResultInputTracker(this.enteredInputs, ({ symbol, barSize, tenor, session, window }) => ({
    key: JSON.stringify([symbol.toUpperCase(), barSize, tenor, SESSION_POLICY_FOR_REQUEST[session], window?.startMs ?? null, window?.endMs ?? null]),
    facts: [
      { label: "Instrument", value: symbol.toUpperCase() },
      { label: "Bar size", value: barSize },
      { label: "Tenor", value: tenor },
      { label: "Session", value: sessionPolicyWords(SESSION_POLICY_FOR_REQUEST[session]) },
      ...(window === null ? [] : [{ label: "Window", value: `${etDate(window.startMs)} to ${etDate(window.endMs)}` }]),
    ],
  }));

  /**
   * How many of the sessions the run reads the lake does not hold yet, and the capture of them. The span is the window the
   * server resolved for this run (a run admits exactly that, with no warmup), in the split-adjusted tree the study reads.
   * Read once per instrument or window; it warns and never holds up Run study.
   */
  readonly holding = createLakeHolding(
    computed<InstrumentDataRequest | null>(() => {
      const window = this.defaultWindow();
      const symbol = this.symbol().trim().toUpperCase();
      if (window === null || symbol === "") return null;
      return { symbol, from: etIsoDate(window.windowStartMs), to: etIsoDate(window.windowEndMs), tree: LAKE_STUDY_TREE };
    }),
  );

  /**
   * The data-source line for what is on screen: the shown result's lake source, or the source a refused run was refused
   * against. `undefined` before either exists, which leaves the study's own pre-run line.
   */
  readonly sourceLine = computed<DataSourceProvenance | undefined>(() => {
    const source = this.data()?.dataSource ?? this.refused()?.dataSource ?? null;
    return source === null ? undefined : toDataSourceProvenance(source);
  });

  /** What step 3 says is about to run. */
  readonly readySummary = computed(() => {
    const window = this.defaultWindow();
    const span = window === null ? "" : `, ${etDate(window.windowStartMs)} to ${etDate(window.windowEndMs)}`;
    return `${this.symbol().toUpperCase()}, ${this.barSize()} bars, ${this.sessionWords()}, ${this.tenor()} tenor${span}`;
  });

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
          session: submitted.session,
          ...(submitted.window === null ? {} : { window: submitted.window }),
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
      // The run filled the lake with whatever it lacked, so the count of what it lacks is read again.
      this.holding.reread();
    }
  }

  /**
   * Sets the inputs from a saved run's. The run recorded the bar size the study was told ("15m" or "1d"); a run asked
   * for any other size was refused and fills in nothing. Its session is the one it names, or the one its bar size read
   * before a run named one (extended for 15-minute bars, regular hours for daily bars); a combination the page no longer
   * offers is refused, never filled as another. Everything is read first, so a field it cannot read leaves the page as it was.
   *
   * It does not fill the window. The saved run's window is not restored: the page reads today's default window for the
   * bar size and session it filled, so a rerun asks about the span ending at the last session that can be read now, not
   * the span the saved run read. The new run's own window is what the page shows before Run study.
   */
  private fillFrom(stored: StoredInputs): void {
    const sent = stored.text("bar_size");
    const barSize = sent === "15m" ? "15m" : sent === "1d" ? "1D" : null;
    if (barSize === null) throw new StoredInputsError(`"${sent}" is not a bar size this page offers`);
    const days = `${stored.number("tenor_days")}D`;
    const tenor = TENORS.find((offered) => offered === days);
    if (tenor === undefined) throw new StoredInputsError(`a ${days} tenor is not offered here`);
    const session = storedSession(stored, barSize);
    this.symbol.set(stored.text("symbol").toUpperCase());
    this.barSize.set(barSize);
    this.tenor.set(tenor);
    if (session !== null) this.session.set(session);
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

/** An ET-midnight `int64 ms UTC` as a date for the page to show (the shared date-only display). The lake reading above is asked
 *  for `etIsoDate`: a display string is never sent to a server. */
function etDate(ms: number): string {
  return formatTimestampDisplay(ms, { mode: "date-et", granularity: "date" });
}

/**
 * The session a saved run read, for the page's one session control. Daily bars read regular hours and fill nothing
 * (`null`); a 15-minute run's HF estimator followed its session, so the two must agree: extended with ETH or regular
 * with RTH. A run from before the session was recorded read the bar size's own, extended hours.
 */
function storedSession(stored: StoredInputs, barSize: BarSize): Session | null {
  const named = stored.has("session") ? stored.text("session") : null;
  if (barSize === "1D") {
    if (named !== null && named !== "rth") throw new StoredInputsError(`daily bars read regular hours only, not "${named}"`);
    return null;
  }
  const session = named ?? "extended";
  if (session !== "rth" && session !== "extended") throw new StoredInputsError(`"${session}" is not a session this page offers`);
  const hf = stored.text("hf_session");
  if (hf !== (session === "rth" ? "RTH" : "ETH")) {
    throw new StoredInputsError(`bars from ${sessionPolicyWords(SESSION_POLICY_FOR_REQUEST[session])} with the HF estimator on ${hf} are not offered here`);
  }
  return session;
}
