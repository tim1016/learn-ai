import { ChangeDetectionStrategy, Component, computed, input, signal } from "@angular/core";
import { TooltipModule } from "primeng/tooltip";
import type {
  RunVerdict,
  RunVerdictGrade,
  RunVerdictSignal,
  RunVerdictSubScore,
} from "../../../api/run-verdict.types";

/** Template view-model derived 1:1 from the backend-authored RunVerdict.
 *  The frontend performs NO scoring — the verdict is frozen at run
 *  completion by PythonDataService/app/services/run_verdict_service.py
 *  (the canonical implementation) and rendered verbatim here. */
interface CardDimension {
  key: string;
  label: string;
  weight: number;
  score: number | null;
  summary: string;
  subScores: RunVerdictSubScore[];
}

interface CardReport {
  composite: number | null;
  grade: RunVerdictGrade | null;
  signal: RunVerdictSignal | null;
  verdict: string;
  dimensions: CardDimension[];
  normalizedWeights: boolean;
}

const EMPTY_REPORT: CardReport = {
  composite: null,
  grade: null,
  signal: null,
  verdict: "No frozen verdict recorded for this run.",
  dimensions: [],
  normalizedWeights: false,
};

@Component({
  selector: "app-readiness-score-card",
  imports: [TooltipModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: "./readiness-score-card.component.html",
  styleUrls: ["./readiness-score-card.component.scss"],
})
export class ReadinessScoreCardComponent {
  /** The persisted, backend-authored run verdict. Null → honest empty card. */
  readonly verdict = input<RunVerdict | null>(null);
  readonly expanded = signal<boolean>(false);

  readonly report = computed<CardReport>(() => {
    const v = this.verdict();
    if (!v) return EMPTY_REPORT;
    return {
      composite: v.composite,
      grade: v.grade,
      signal: v.signal,
      verdict: v.headline,
      dimensions: v.dimensions.map((d) => ({
        key: d.key,
        label: d.label,
        weight: d.weight,
        score: d.score,
        summary: d.summary,
        subScores: d.sub_scores,
      })),
      normalizedWeights: v.normalized_weights,
    };
  });

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

  gradeChipClass(grade: RunVerdictGrade | null): string {
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
