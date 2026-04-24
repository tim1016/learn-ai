import {
  ChangeDetectionStrategy,
  Component,
  computed,
  DestroyRef,
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

import {
  Advisory,
  AdvisoryAction,
  AvailabilityCell,
  computeAdvisories,
  daysBetween,
  isoDate,
  Resolution,
  summarizeAvailability,
  TickerOption,
  TickerRange,
} from "./ticker-range-picker.types";

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
  imports: [CommonModule, FormsModule],
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
  readonly title = input("Ticker & range");

  /** Emitted when the user clicks an advisory action button. The patch
   *  portion is auto-applied to ``value`` before the event fires — the
   *  host only needs to react to the side-effect flags
   *  (``triggerRun`` / ``refetchHoles``). */
  readonly advisoryAction = output<AdvisoryAction>();

  private readonly destroyRef = inject(DestroyRef);
  private readonly rootEl =
    viewChild.required<ElementRef<HTMLElement>>("rootEl");

  readonly open = signal(false);
  readonly query = signal("");

  readonly summary = computed(() => summarizeAvailability(this.availability()));
  readonly advisories = computed<readonly Advisory[]>(() =>
    computeAdvisories(this.value(), this.summary())
  );
  readonly spanDays = computed(() => {
    const v = this.value();
    return daysBetween(v.from, v.to);
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
