import {
  ChangeDetectionStrategy,
  Component,
  computed,
  DestroyRef,
  effect,
  ElementRef,
  HostListener,
  inject,
  input,
  model,
  output,
  signal,
  viewChild,
} from "@angular/core";
import { CommonModule } from "@angular/common";
import { FormsModule } from "@angular/forms";
import { Tooltip } from "primeng/tooltip";

import {
  Advisory,
  AdvisoryAction,
  AvailabilityCell,
  computeAdvisories,
  daysBetween,
  DominantState,
  dominantState,
  isoDate,
  Resolution,
  Session,
  summarizeAvailability,
  TickerOption,
  TickerRange,
  weekdaysBetween,
} from "./ticker-range-picker.types";

/** Visual treatment for the smart-availability legend.
 *  ``tinted-bold`` (default) = swatch + count + label on tinted bg.
 *  ``solid-bold``           = filled chip in state colour, white label.
 *  ``icon-glyph``           = icon + count, no swatch. Quietest. */
export type LegendTreatment = "tinted-bold" | "solid-bold" | "icon-glyph";

/** Session-toggle behaviour for the picker.
 *  ``preview``  = Both options visible; "preview" tag on Extended; both
 *                 selectable in the UI but consumers may ignore Extended.
 *  ``disabled`` = Extended rendered but disabled with a tooltip.
 *  ``hidden``   = Session group not rendered at all. */
export type SessionMode = "preview" | "disabled" | "hidden";

interface Preset { days: number; label: string }
const PRESETS: readonly Preset[] = [
  { days: 7, label: "7D" },
  { days: 30, label: "1M" },
  { days: 90, label: "3M" },
  { days: 180, label: "6M" },
  { days: 365, label: "1Y" },
  { days: 730, label: "2Y" },
];

/**
 * Display names for common US-equity venues. Surfaced in the exchange-chip
 * tooltip so users see "NYSE Arca" instead of just "ARCA". Keep the keys
 * upper-cased to match Polygon's primary_exchange values.
 */
const EXCHANGE_NAMES: Readonly<Record<string, string>> = {
  ARCA: "NYSE Arca",
  NASDAQ: "NASDAQ",
  NYSE: "New York Stock Exchange",
  BATS: "Cboe BZX",
  IEX: "IEX",
  AMEX: "NYSE American",
};

/**
 * Shared ticker + range picker.
 *
 * Inputs (two-way on ``value``) encapsulate the whole picker state:
 *
 *   symbol, from, to, resolution, autoFetch
 *
 * Behaviour:
 *   - Symbol combobox with search + recent list + cache completeness hint
 *   - Smart default: picking a ticker snaps ``from``/``to`` to the last
 *     30 days of that ticker's cache (via ``TickerOption.last``)
 *   - Quick-range presets (7D / 1M / 3M / 6M / 1Y)
 *   - Resolution tri-toggle (minute / hour / daily) scoped by
 *     ``availableResolutions``
 *   - Availability strip — the caller passes per-day status cells; the
 *     picker draws them and derives a compact summary line
 *   - Smart advisories (quiet badges) — suggest resolution downgrades,
 *     warn about unfetched data, bad-mark wide minute ranges, etc.
 *     Each has a one-click patch action; the picker emits
 *     ``advisoryAction`` for side-effects the host must handle (e.g.
 *     triggerRun, refetchHoles).
 */
@Component({
  selector: "app-ticker-range-picker",
  imports: [CommonModule, FormsModule, Tooltip],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: "./ticker-range-picker.component.html",
  styleUrls: ["./ticker-range-picker.component.scss"],
})
export class TickerRangePickerComponent {
  /** Two-way-bound picker state. Host binds
   *  ``[(value)]="rangeState"``. */
  readonly value = model.required<TickerRange>();

  /** Full list of selectable tickers. Host fetches this once (from its
   *  tracked-instruments endpoint) and passes it in. */
  readonly tickerPool = input<readonly TickerOption[]>([]);

  /** Recently-used symbols — shown at the top of the dropdown when the
   *  search box is empty. */
  readonly recent = input<readonly string[]>([]);

  /** Per-day availability cells for the current range. Host is expected
   *  to refetch this when (symbol, from, to, resolution) changes. If
   *  empty, the strip renders a neutral placeholder. */
  readonly availability = input<readonly AvailabilityCell[]>([]);

  /** Which resolutions the current page supports. */
  readonly availableResolutions = input<readonly Resolution[]>([
    "minute",
    "hour",
    "daily",
  ]);

  readonly showAutoFetch = input(true);

  /** When true, the resolution tri-toggle is hidden. Use when the host
   *  page has its own, richer resolution control (e.g. Data Lab's
   *  bar-timeframe dropdown) and only needs the picker for
   *  symbol/date/availability/advisories. */
  readonly hideResolution = input(false);

  /** Title shown in the tiny uppercase label at top of the card. */
  readonly title = input("Backtest data");

  /** Visual treatment for the smart-availability legend. */
  readonly legendTreatment = input<LegendTreatment>("tinted-bold");

  /** How to render the session toggle. ``preview`` shows both RTH and
   *  Ext with a "preview" tag (default — used by Engine Lab while the
   *  Python engine still hardcodes RTH). */
  readonly sessionMode = input<SessionMode>("preview");

  /** Emitted when the user clicks an advisory action button. The patch
   *  portion is auto-applied to ``value`` before the event fires — the
   *  host only needs to react to the side-effect flags
   *  (``triggerRun`` / ``refetchHoles``). */
  readonly advisoryAction = output<AdvisoryAction>();

  private readonly destroyRef = inject(DestroyRef);
  private readonly rootEl =
    viewChild.required<ElementRef<HTMLElement>>("rootEl");
  private readonly searchInput =
    viewChild<ElementRef<HTMLInputElement>>("searchInput");

  readonly open = signal(false);
  readonly query = signal("");

  constructor() {
    // When the dropdown opens, the search input is freshly rendered. The
    // user's click that opened it landed on the ticker-box (or an Enter/
    // Space keystroke), so the input doesn't have focus yet. Pull focus
    // on the next microtask so they can type immediately.
    effect(() => {
      if (this.open()) {
        const input = this.searchInput();
        if (input) {
          queueMicrotask(() => input.nativeElement.focus());
        }
      }
    });
  }

  readonly summary = computed(() => summarizeAvailability(this.availability()));

  /** Which state the smart legend should emphasize. */
  readonly dominant = computed<DominantState>(() => dominantState(this.summary()));

  /** Convenience: the {@link TickerOption} that matches the current symbol,
   *  or undefined when the pool hasn't loaded yet. */
  readonly selectedTicker = computed<TickerOption | undefined>(() =>
    this.tickerPool().find((t) => t.symbol === this.value().symbol)
  );

  /** Cache pct + last-cached date for the right-side helper inside the
   *  Instrument group. Both null when the symbol isn't in the pool. */
  readonly selectedTickerCachePct = computed<number | null>(() => {
    const cache = this.selectedTicker()?.cache;
    return typeof cache === "number" ? cache : null;
  });
  readonly selectedTickerLast = computed<string | null>(() => {
    const last = this.selectedTicker()?.last ?? null;
    return last;
  });
  readonly advisories = computed<readonly Advisory[]>(() =>
    computeAdvisories(this.value(), this.summary())
  );
  readonly spanDays = computed(() => {
    const v = this.value();
    return daysBetween(v.from, v.to);
  });
  /**
   * Business-days in the picker's range. Prefers the availability-cell
   * count when cells are supplied by the host (so it reflects the actual
   * market calendar, excluding holidays). Falls back to a plain
   * weekday count between ``from`` and ``to`` when no cells exist — this
   * is what Data Lab hits today (it doesn't yet fetch a per-day
   * availability report), and without the fallback the readout shows
   * ``0bd`` for every range.
   */
  readonly spanBusinessDays = computed(() => {
    const summaryDays = this.summary().weekdays;
    if (summaryDays > 0) return summaryDays;
    const v = this.value();
    return weekdaysBetween(v.from, v.to);
  });
  readonly activePreset = computed(() => {
    const s = this.spanDays();
    return PRESETS.find((p) => Math.abs(s - p.days) < 2)?.days ?? null;
  });

  readonly selectedExchange = computed(
    () =>
      this.tickerPool().find((t) => t.symbol === this.value().symbol)
        ?.exchange ?? "—"
  );

  /**
   * Hover/focus tooltip for the exchange chip. Ticker-aware: we look up
   * the chip's code in the EXCHANGE_NAMES map so the user sees
   * "NYSE Arca — primary listing venue for SPY" instead of the bare
   * code. Falls back to a generic explainer when the code is unknown.
   */
  readonly selectedExchangeTooltip = computed<string>(() => {
    const code = this.selectedExchange();
    const symbol = this.value().symbol;
    const name = EXCHANGE_NAMES[code];
    if (!name) {
      return "Listing exchange — where this instrument is primarily traded.";
    }
    return `${name} — primary listing venue for ${symbol}.`;
  });

  readonly filteredTickers = computed<readonly TickerOption[]>(() => {
    const q = this.query().trim().toUpperCase();
    const pool = this.tickerPool();
    if (!q) return pool;
    return pool.filter(
      (t) =>
        t.symbol.includes(q) || t.name.toUpperCase().includes(q)
    );
  });

  readonly recentTickers = computed<readonly TickerOption[]>(() => {
    const recent = this.recent();
    if (recent.length === 0) return [];
    const pool = this.tickerPool();
    return recent
      .map((s) => pool.find((t) => t.symbol === s))
      .filter((t): t is TickerOption => !!t);
  });

  readonly presets = PRESETS;

  trackBySymbol(_: number, t: TickerOption): string {
    return t.symbol;
  }

  trackByDay(_: number, c: AvailabilityCell): string {
    return c.date;
  }

  openDropdown(): void {
    if (this.open()) {
      // Already open — don't re-trigger and wipe the user's in-progress query.
      return;
    }
    this.open.set(true);
    this.query.set("");
  }

  closeDropdown(): void {
    this.open.set(false);
  }

  /**
   * Enter / Space on the ticker-box should open the dropdown — unless
   * it's already open, in which case the key event belongs to the
   * search input and we just let it through.
   */
  onTickerBoxEnter(event: Event): void {
    if (!this.open()) {
      this.openDropdown();
      event.preventDefault();
    }
  }

  onTickerBoxSpace(event: Event): void {
    if (!this.open()) {
      this.openDropdown();
      event.preventDefault();
    }
  }

  @HostListener("document:mousedown", ["$event"])
  onDocumentMouseDown(event: MouseEvent): void {
    const host = this.rootEl().nativeElement;
    if (!host.contains(event.target as Node)) {
      this.closeDropdown();
    }
  }

  onSearchInput(value: string): void {
    this.query.set(value);
  }

  /**
   * On ticker pick, we also jump ``from``/``to`` to the last 30 days of
   * that ticker's cache — the single most common thing a user wants to
   * look at right after picking a new symbol. If the ticker has no
   * cache, the previous range is preserved.
   */
  pickTicker(t: TickerOption): void {
    const current = this.value();
    const patch: Partial<TickerRange> = { symbol: t.symbol };
    if (t.last) {
      const end = new Date(t.last);
      const start = new Date(end);
      start.setDate(start.getDate() - 30);
      patch.from = isoDate(start);
      patch.to = isoDate(end);
    }
    this.value.set({ ...current, ...patch });
    this.closeDropdown();
  }

  updateFrom(v: string): void {
    this.value.set({ ...this.value(), from: v });
  }

  updateTo(v: string): void {
    this.value.set({ ...this.value(), to: v });
  }

  setResolution(r: Resolution): void {
    this.value.set({ ...this.value(), resolution: r });
  }

  /** Effective session — defaults to ``rth`` so legacy callers that
   *  don't set the field render correctly. */
  readonly effectiveSession = computed<Session>(
    () => this.value().session ?? "rth"
  );

  setSession(s: Session): void {
    // Ignore Extended clicks while disabled — UI guards visually but we
    // double-check here so the host's two-way binding never receives a
    // value the backend can't honor.
    if (s === "extended" && this.sessionMode() === "disabled") return;
    this.value.set({ ...this.value(), session: s });
  }

  applyPreset(days: number): void {
    const end = new Date();
    end.setHours(0, 0, 0, 0);
    const start = new Date(end);
    start.setDate(start.getDate() - days);
    this.value.set({
      ...this.value(),
      from: isoDate(start),
      to: isoDate(end),
    });
  }

  setAutoFetch(on: boolean): void {
    this.value.set({ ...this.value(), autoFetch: on });
  }

  /**
   * Click handler for an advisory's action button. Applies the patch
   * (if any) to ``value`` and re-emits the action so the host can honor
   * side-effect flags like ``triggerRun``.
   */
  onAdvisoryAction(action: AdvisoryAction): void {
    if (action.patch) {
      this.value.set({ ...this.value(), ...action.patch });
    }
    this.advisoryAction.emit(action);
  }

  cacheTextColor(pct: number | undefined): string {
    if (pct === undefined) return "var(--text-muted)";
    if (pct >= 0.9) return "var(--bull)";
    if (pct >= 0.5) return "var(--warn)";
    return "var(--text-muted)";
  }

  cacheLabel(pct: number | undefined): string {
    if (pct === undefined || pct === 0) return "no cache";
    return `${Math.round(pct * 100)}%`;
  }
}
