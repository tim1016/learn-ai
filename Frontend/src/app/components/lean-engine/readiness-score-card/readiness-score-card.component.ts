import { ChangeDetectionStrategy, Component, computed, input, signal } from "@angular/core";
import { CommonModule } from "@angular/common";
import { TooltipModule } from "primeng/tooltip";
import {
  ReadinessResultLike,
  computeReadiness,
  type Grade,
} from "./readiness-score.util";

@Component({
  selector: "app-readiness-score-card",
  standalone: true,
  imports: [CommonModule, TooltipModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: "./readiness-score-card.component.html",
  styleUrls: ["./readiness-score-card.component.scss"],
})
export class ReadinessScoreCardComponent {
  readonly result = input<ReadinessResultLike | null>(null);
  readonly expanded = signal<boolean>(false);

  readonly report = computed(() => computeReadiness(this.result()));

  readonly hasScore = computed<boolean>(() => this.report().composite !== null);

  /** Green / amber / red band from the composite score. Used for the ring
   *  color and the grade chip background. */
  readonly band = computed<"green" | "amber" | "red" | "na">(() => {
    const s = this.report().composite;
    if (s === null) return "na";
    if (s >= 70) return "green";
    if (s >= 55) return "amber";
    return "red";
  });

  /** SVG stroke-dashoffset for the score ring (circumference ≈ 301.6 for r=48). */
  readonly ringOffset = computed<number>(() => {
    const circumference = 2 * Math.PI * 48;
    const s = this.report().composite ?? 0;
    return circumference * (1 - Math.min(100, Math.max(0, s)) / 100);
  });
  readonly ringCircumference = 2 * Math.PI * 48;

  gradeChipClass(grade: Grade | null): string {
    if (grade === null) return "chip-na";
    if (grade === "A+" || grade === "A") return "chip-green";
    if (grade === "B") return "chip-amber";
    return "chip-red";
  }

  dimensionBand(score: number | null): "green" | "amber" | "red" | "na" {
    if (score === null) return "na";
    if (score >= 70) return "green";
    if (score >= 55) return "amber";
    return "red";
  }

  toggleExpanded(): void {
    this.expanded.update((v) => !v);
  }
}
