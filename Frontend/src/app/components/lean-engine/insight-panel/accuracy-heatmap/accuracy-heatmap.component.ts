import { ChangeDetectionStrategy, Component, computed, input } from "@angular/core";
import { CommonModule } from "@angular/common";

export interface HourRow {
  hour: number;
  count: number;
  correct: number;
  accuracy: number;
}

interface HourCell {
  hour: number;
  count: number;
  accuracy: number | null; // null when count === 0
  band: "red" | "amber" | "green" | "empty";
  isWorst: boolean;
  label: string; // e.g. "09:00"
  tooltip: string;
}

/**
 * Accuracy-by-hour heatmap — renders 24 cells (00:00 – 23:00) colored
 * by directional-signal accuracy. Cells with <40% accuracy are flagged
 * as "no-trade zones" with a red underline and enumerated below the grid.
 */
@Component({
  selector: "app-accuracy-heatmap",
  standalone: true,
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: "./accuracy-heatmap.component.html",
  styleUrls: ["./accuracy-heatmap.component.scss"],
})
export class AccuracyHeatmapComponent {
  readonly rows = input<HourRow[]>([]);

  readonly cells = computed<HourCell[]>(() => {
    const byHour = new Map<number, HourRow>();
    for (const r of this.rows()) byHour.set(r.hour, r);

    // Find worst hour (minimum accuracy with at least one sample)
    const scored = this.rows().filter((r) => r.count > 0);
    const minAcc = scored.length > 0 ? Math.min(...scored.map((r) => r.accuracy)) : 1;
    const worstThreshold = minAcc;

    const cells: HourCell[] = [];
    for (let h = 0; h < 24; h++) {
      const row = byHour.get(h);
      if (!row || row.count === 0) {
        cells.push({
          hour: h,
          count: 0,
          accuracy: null,
          band: "empty",
          isWorst: false,
          label: hourLabel(h),
          tooltip: `${hourLabel(h)} — no samples`,
        });
        continue;
      }
      const band = accuracyBand(row.accuracy);
      cells.push({
        hour: h,
        count: row.count,
        accuracy: row.accuracy,
        band,
        isWorst: row.accuracy === worstThreshold && row.accuracy < 0.5,
        label: hourLabel(h),
        tooltip: `${hourLabel(h)} — ${(row.accuracy * 100).toFixed(1)}% accuracy (${row.correct}/${row.count})`,
      });
    }
    return cells;
  });

  readonly noTradeZones = computed<HourCell[]>(() =>
    this.cells().filter((c) => c.band === "red" && c.accuracy !== null),
  );

  readonly hasSamples = computed<boolean>(() =>
    this.cells().some((c) => c.count > 0),
  );
}

function accuracyBand(a: number): HourCell["band"] {
  if (a < 0.4) return "red";
  if (a < 0.55) return "amber";
  return "green";
}

function hourLabel(h: number): string {
  return `${String(h).padStart(2, "0")}:00`;
}
