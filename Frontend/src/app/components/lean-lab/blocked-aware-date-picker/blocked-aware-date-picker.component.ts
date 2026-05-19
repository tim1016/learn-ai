import { CommonModule } from "@angular/common";
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  model,
  output,
  signal,
} from "@angular/core";
import { LeanSidecarService } from "../../../services/lean-sidecar.service";
import type { BlockedDateEntry } from "../../../services/lean-sidecar.types";

/**
 * P2.5 blocked-aware date picker for the LEAN Lab.
 *
 * Implements the contract from
 * docs/design/p1-4-p2-5/Date Picker Mocks.html — six states (default,
 * popover open, half-day tooltip, DST boundary, invalid-window
 * rejection, computed [start, end) detail).
 *
 * The picker fetches blocked dates from
 * GET /api/lean-sidecar/calendar/blocked-dates so weekends, holidays,
 * and half-days are disabled in the month grid AND client-side
 * validated before submit. The native <input type="date"> can't disable
 * specific dates per the v2 constraint; this component owns its own
 * popover.
 *
 * Two-way binding via ``startDate`` / ``endDate`` ``model()`` signals,
 * ISO ``YYYY-MM-DD`` strings — matches the existing form-control type.
 */
@Component({
  selector: "app-blocked-aware-date-picker",
  standalone: true,
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  styleUrls: ["./blocked-aware-date-picker.component.scss"],
  template: `
    <div class="picker-host" [class.picker-host--invalid]="invalidWindow()">
      <div class="picker-inputs">
        <button
          type="button"
          class="picker-input"
          (click)="openPopoverFor('start')"
          [attr.aria-pressed]="openFor() === 'start'"
        >
          <span class="picker-input__label">Start</span>
          <span class="picker-input__value mono">{{ startDate() || "Pick start" }}</span>
          <span class="picker-input__caret">▾</span>
        </button>
        <span class="picker-inputs__arrow">→</span>
        <button
          type="button"
          class="picker-input"
          (click)="openPopoverFor('end')"
          [attr.aria-pressed]="openFor() === 'end'"
        >
          <span class="picker-input__label">End</span>
          <span class="picker-input__value mono">{{ endDate() || "Pick end" }}</span>
          <span class="picker-input__caret">▾</span>
        </button>
      </div>

      @if (openFor() !== null) {
        <div class="picker-popover" role="dialog" aria-label="Pick a trading date">
          <header class="picker-popover__head">
            <button
              type="button"
              class="picker-popover__nav"
              (click)="navMonth(-1)"
              aria-label="Previous month"
            >‹</button>
            <span class="picker-popover__month mono">{{ monthLabel() }}</span>
            <button
              type="button"
              class="picker-popover__nav"
              (click)="navMonth(1)"
              aria-label="Next month"
            >›</button>
          </header>
          <div class="picker-popover__weekdays">
            @for (w of ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"]; track w) {
              <span>{{ w }}</span>
            }
          </div>
          <div class="picker-popover__grid">
            @for (cell of monthCells(); track cell.iso) {
              <button
                type="button"
                class="picker-cell"
                [class.picker-cell--blank]="cell.outOfMonth"
                [class.picker-cell--weekend]="cell.reason === 'weekend'"
                [class.picker-cell--holiday]="cell.reason === 'holiday'"
                [class.picker-cell--halfday]="cell.reason === 'early_close'"
                [class.picker-cell--selected]="isCellInRange(cell.iso)"
                [disabled]="cell.outOfMonth || cell.reason !== null"
                [title]="cell.tooltip"
                (click)="onCellClick(cell.iso)"
              >
                @if (!cell.outOfMonth) {
                  <span class="picker-cell__day">{{ cell.dayNum }}</span>
                  @if (cell.reason === "holiday") { <span class="picker-cell__badge">H</span> }
                  @if (cell.reason === "early_close") { <span class="picker-cell__badge">½</span> }
                }
              </button>
            }
          </div>
          <footer class="picker-popover__legend">
            <span class="legend-chip legend-chip--holiday">H holiday</span>
            <span class="legend-chip legend-chip--halfday">½ half-day</span>
            <span class="legend-chip legend-chip--weekend">weekend</span>
            @if (dstAdvisory(); as ad) {
              <span class="legend-chip legend-chip--dst">DST · {{ ad }}</span>
            }
          </footer>
        </div>
      }

      @if (advisory(); as ad) {
        <div class="picker-advisory" [class.picker-advisory--bad]="ad.kind === 'bad'" [class.picker-advisory--info]="ad.kind === 'info'">
          <span class="picker-advisory__sigil">{{ ad.kind === "bad" ? "⨯" : "ⓘ" }}</span>
          <span class="picker-advisory__msg">{{ ad.text }}</span>
        </div>
      }

      @if (showComputed()) {
        <details class="picker-computed">
          <summary>Show computed window</summary>
          <div class="picker-computed__body mono">
            <div>start_date {{ startDate() }} → session open 09:30 ET</div>
            <div>end_date {{ endDate() }} → next_trading_day = {{ exclusiveEndIso() }}</div>
            <div>start_ms_utc {{ startMsUtc() }}</div>
            <div>end_ms_utc {{ endMsUtc() }}</div>
            <div>window = [ {{ startMsUtc() }} , {{ endMsUtc() }} )</div>
          </div>
        </details>
      }
    </div>
  `,
})
export class BlockedAwareDatePickerComponent {
  private readonly service = inject(LeanSidecarService);

  readonly startDate = model.required<string>();
  readonly endDate = model.required<string>();

  /** Show the expandable computed-window detail (State F). Off by default. */
  readonly showComputed = input(true);

  /** Emit when the picker computes an invalid window so the host can
   *  disable submit. */
  readonly windowInvalid = output<boolean>();

  /** Which input opened the popover; null when closed. */
  readonly openFor = signal<"start" | "end" | null>(null);

  /** Current month displayed in the popover (1st-of-month ISO). */
  readonly viewMonth = signal<string>(this.todayMonthIso());

  /** Server-fetched blocked dates for the currently visible month. */
  private readonly blockedByDate = signal<Map<string, BlockedDateEntry["reason"]>>(new Map());

  /** Last error from the calendar endpoint (rare; surfaced inline). */
  readonly calendarError = signal<string | null>(null);

  /** Month label for the popover header. */
  readonly monthLabel = computed(() => {
    const iso = this.viewMonth();
    const d = this.parseIsoLocal(iso);
    return d.toLocaleString("en-US", { month: "long", year: "numeric" });
  });

  /** 6×7 grid for the visible month. Days outside the month are
   *  "blank" placeholders so the grid stays aligned. */
  readonly monthCells = computed(() => {
    const monthIso = this.viewMonth();
    const blocked = this.blockedByDate();
    const monthStart = this.parseIsoLocal(monthIso);
    const firstWeekday = monthStart.getDay();
    const cells: PickerCell[] = [];
    // Leading blanks.
    for (let i = 0; i < firstWeekday; i++) {
      cells.push({ iso: `blank-pre-${i}`, dayNum: 0, outOfMonth: true, reason: null, tooltip: "" });
    }
    // Days in month.
    const next = new Date(monthStart);
    next.setMonth(next.getMonth() + 1);
    const daysInMonth = Math.round(
      (next.getTime() - monthStart.getTime()) / 86_400_000,
    );
    for (let day = 1; day <= daysInMonth; day++) {
      const dt = new Date(monthStart);
      dt.setDate(day);
      const iso = this.toIsoLocal(dt);
      const reason = blocked.get(iso) ?? null;
      cells.push({
        iso,
        dayNum: day,
        outOfMonth: false,
        reason,
        tooltip: this.tooltipFor(iso, reason),
      });
    }
    // Trailing blanks to fill the grid to 42 cells (6 weeks).
    while (cells.length < 42) {
      cells.push({
        iso: `blank-post-${cells.length}`,
        dayNum: 0,
        outOfMonth: true,
        reason: null,
        tooltip: "",
      });
    }
    return cells;
  });

  /** True iff start ≤ end and no blocked date sits inside the window. */
  readonly invalidWindow = computed(() => {
    const start = this.startDate();
    const end = this.endDate();
    if (!start || !end) return false;
    if (start > end) return true;
    const blocked = this.blockedByDate();
    // Endpoint check.
    if (blocked.get(start) === "early_close") return true;
    if (blocked.get(end) === "early_close") return true;
    if (blocked.has(start) && blocked.get(start) !== "weekend") return true;
    if (blocked.has(end) && blocked.get(end) !== "weekend") return true;
    // Inside check: any early_close inside the window is fatal.
    const cursor = this.parseIsoLocal(start);
    const endDt = this.parseIsoLocal(end);
    while (cursor <= endDt) {
      const iso = this.toIsoLocal(cursor);
      if (blocked.get(iso) === "early_close") return true;
      cursor.setDate(cursor.getDate() + 1);
    }
    return false;
  });

  /** State E — surface an inline advisory naming the rejection rule. */
  readonly advisory = computed<{ kind: "bad" | "info"; text: string } | null>(() => {
    if (this.invalidWindow()) {
      // Find the first blocking date inside [start, end].
      const blocked = this.blockedByDate();
      const cursor = this.parseIsoLocal(this.startDate());
      const endDt = this.parseIsoLocal(this.endDate());
      while (cursor <= endDt) {
        const iso = this.toIsoLocal(cursor);
        const reason = blocked.get(iso);
        if (reason === "early_close") {
          return {
            kind: "bad",
            text: `Window touches a half-day. ${iso} closes at 13:00 ET. P2.5 contract requires the window to consist of full sessions only.`,
          };
        }
        if (reason === "holiday" && (iso === this.startDate() || iso === this.endDate())) {
          return {
            kind: "bad",
            text: `${iso} is a NYSE holiday. The window's start and end must each be a full trading session.`,
          };
        }
        cursor.setDate(cursor.getDate() + 1);
      }
      return { kind: "bad", text: "Window is invalid — pick full trading sessions on both endpoints." };
    }
    if (this.dstAdvisory()) {
      return {
        kind: "info",
        text: `Daylight Saving Time boundary in range. Session open stays at 09:30 ET; UTC-ms boundary shifts by 1h. All bars resolve through pandas_market_calendars — no fixed-offset math.`,
      };
    }
    return null;
  });

  /** State D — true when a DST transition falls inside [start, end]. */
  readonly dstAdvisory = computed<string | null>(() => {
    const start = this.startDate();
    const end = this.endDate();
    if (!start || !end) return null;
    // DST flips are the second Sunday of March and the first Sunday of
    // November. Detect by comparing the formatted NY UTC-offset at the
    // two endpoints — a fixed-offset converter would produce the same
    // value on both and miss the boundary.
    const a = this.utcOffsetMinutes(start);
    const b = this.utcOffsetMinutes(end);
    if (a !== b) return "transitions";
    return null;
  });

  /** Exclusive-end ISO date: next_trading_day(endDate) per the calendar. */
  readonly exclusiveEndIso = computed(() => {
    const end = this.endDate();
    if (!end) return "";
    const blocked = this.blockedByDate();
    const cursor = this.parseIsoLocal(end);
    cursor.setDate(cursor.getDate() + 1);
    // Walk forward until we hit a date that is NOT blocked.
    for (let safety = 0; safety < 14; safety++) {
      const iso = this.toIsoLocal(cursor);
      if (!blocked.has(iso)) return iso;
      cursor.setDate(cursor.getDate() + 1);
    }
    // Fallback: validator will surface a 422 if this is wrong.
    return this.toIsoLocal(cursor);
  });

  readonly startMsUtc = computed(() => this.isoToSessionOpenMsUtc(this.startDate()));
  readonly endMsUtc = computed(() => this.isoToSessionOpenMsUtc(this.exclusiveEndIso()));

  constructor() {
    void this.refreshBlockedDates();
  }

  // ── User interaction ────────────────────────────────────────────────

  openPopoverFor(which: "start" | "end"): void {
    this.openFor.set(this.openFor() === which ? null : which);
    if (this.openFor() === which) {
      const anchor = which === "start" ? this.startDate() : this.endDate();
      if (anchor) {
        this.viewMonth.set(this.monthIsoFor(anchor));
      }
      void this.refreshBlockedDates();
    }
  }

  navMonth(delta: number): void {
    const d = this.parseIsoLocal(this.viewMonth());
    d.setMonth(d.getMonth() + delta);
    this.viewMonth.set(this.toIsoLocal(d));
    void this.refreshBlockedDates();
  }

  onCellClick(iso: string): void {
    const target = this.openFor();
    if (target === null) return;
    if (target === "start") {
      this.startDate.set(iso);
      this.openFor.set("end");
    } else {
      this.endDate.set(iso);
      this.openFor.set(null);
    }
    this.windowInvalid.emit(this.invalidWindow());
  }

  isCellInRange(iso: string): boolean {
    const start = this.startDate();
    const end = this.endDate();
    if (!start || !end) return iso === start;
    return iso >= start && iso <= end;
  }

  // ── Helpers ─────────────────────────────────────────────────────────

  private async refreshBlockedDates(): Promise<void> {
    const viewIso = this.viewMonth();
    const view = this.parseIsoLocal(viewIso);
    // Fetch a generous 3-month window centered on the view so nav
    // arrows don't require an immediate refetch.
    const from = new Date(view);
    from.setMonth(from.getMonth() - 1);
    const to = new Date(view);
    to.setMonth(to.getMonth() + 2);
    to.setDate(0); // last day of the 2nd-following month
    try {
      const payload = await this.service.getBlockedDates(
        this.toIsoLocal(from),
        this.toIsoLocal(to),
      );
      const map = new Map<string, BlockedDateEntry["reason"]>();
      for (const entry of payload.blocked) {
        map.set(entry.date, entry.reason);
      }
      this.blockedByDate.set(map);
      this.calendarError.set(null);
      this.windowInvalid.emit(this.invalidWindow());
    } catch (err) {
      this.calendarError.set(err instanceof Error ? err.message : String(err));
    }
  }

  private tooltipFor(iso: string, reason: BlockedDateEntry["reason"] | null): string {
    if (reason === "weekend") return `${iso} — weekend`;
    if (reason === "holiday") return `${iso} — NYSE holiday`;
    if (reason === "early_close") {
      return `${iso} — half-day · NYSE closes at 13:00 ET. P2.5 contract requires full sessions only; this date is rejected at submit.`;
    }
    return iso;
  }

  /** Compute the NY UTC offset (minutes) for 09:30 ET on ``iso``. */
  private utcOffsetMinutes(iso: string): number {
    const [y, m, d] = iso.split("-").map((n) => Number.parseInt(n, 10));
    const refUtcMs = Date.UTC(y, m - 1, d, 12, 0, 0);
    const fmt = new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      timeZoneName: "longOffset",
    });
    const parts = fmt.formatToParts(new Date(refUtcMs));
    const offsetPart = parts.find((p) => p.type === "timeZoneName")?.value ?? "";
    const match = offsetPart.match(/GMT([+-])(\d{2}):(\d{2})/);
    if (!match) return 0;
    const sign = match[1] === "-" ? -1 : 1;
    return sign * (Number.parseInt(match[2], 10) * 60 + Number.parseInt(match[3], 10));
  }

  /** ISO ``YYYY-MM-DD`` → ms UTC of 09:30 ET on that date. */
  private isoToSessionOpenMsUtc(iso: string): number {
    if (!iso) return 0;
    const [y, m, d] = iso.split("-").map((n) => Number.parseInt(n, 10));
    if (!Number.isFinite(y) || !Number.isFinite(m) || !Number.isFinite(d)) return 0;
    const offsetMs = this.utcOffsetMinutes(iso) * 60_000;
    return Date.UTC(y, m - 1, d, 9, 30, 0) - offsetMs;
  }

  private parseIsoLocal(iso: string): Date {
    const [y, m, d] = iso.split("-").map((n) => Number.parseInt(n, 10));
    return new Date(y, (m ?? 1) - 1, d ?? 1);
  }

  private toIsoLocal(d: Date): string {
    return [
      d.getFullYear(),
      String(d.getMonth() + 1).padStart(2, "0"),
      String(d.getDate()).padStart(2, "0"),
    ].join("-");
  }

  private monthIsoFor(iso: string): string {
    const d = this.parseIsoLocal(iso);
    return [d.getFullYear(), String(d.getMonth() + 1).padStart(2, "0"), "01"].join("-");
  }

  private todayMonthIso(): string {
    const now = new Date();
    return [now.getFullYear(), String(now.getMonth() + 1).padStart(2, "0"), "01"].join("-");
  }
}

interface PickerCell {
  iso: string;
  dayNum: number;
  outOfMonth: boolean;
  reason: BlockedDateEntry["reason"] | null;
  tooltip: string;
}
