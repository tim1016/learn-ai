import { ChangeDetectionStrategy, Component, computed, input } from "@angular/core";

import type { RunVerdict, RunVerdictMissingEvidence } from "../../../api/run-verdict.types";

interface MissingEvidenceView {
  label: string;
  reason: string;
}

/**
 * Displays the persisted Python verdict without deriving a metric band, cause,
 * or trade recommendation in Angular.
 */
@Component({
  selector: "app-strategy-lab-evidence-grade",
  templateUrl: "./evidence-grade.component.html",
  styleUrl: "./evidence-grade.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class EvidenceGradeComponent {
  readonly verdict = input<RunVerdict | null>(null);

  readonly missingEvidence = computed<MissingEvidenceView[]>(() => {
    const verdict = this.verdict();
    if (!verdict) return [];

    const persisted = verdict.missing_required_evidence;
    if (persisted?.length) return persisted.map(toMissingEvidenceView);

    return (verdict.missing_required_metrics ?? []).map((label) => ({
      label,
      reason: "The producer-specific unavailable reason was not recorded for this legacy run.",
    }));
  });

  readonly coverage = computed(() => {
    const verdict = this.verdict();
    if (!verdict || !verdict.required_metrics) return null;
    return `${verdict.available_required_metrics ?? 0}/${verdict.required_metrics}`;
  });
}

function toMissingEvidenceView(item: RunVerdictMissingEvidence): MissingEvidenceView {
  return { label: item.label, reason: item.reason };
}
