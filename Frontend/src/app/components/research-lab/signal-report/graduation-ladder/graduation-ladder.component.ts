import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { TooltipModule } from 'primeng/tooltip';
import {
  GraduationStageInfo,
  Stage0Rejection,
  StageAdvanceCriterion,
} from '../../../../services/research.service';

interface LadderNode {
  stage: 0 | 1 | 2 | 3;
  label: string;
  shortLabel: string;
  /** "current" | "passed" | "next" | "future" — drives the visual treatment. */
  state: 'current' | 'passed' | 'next' | 'future';
  /** Description shown in the tooltip when the node has focus / is hovered. */
  tooltip: string;
}

const STAGE_LABELS: Record<0 | 1 | 2 | 3, { full: string; short: string }> = {
  0: { full: 'Stage 0 — Rejected', short: 'Rejected' },
  1: { full: 'Stage 1 — Weak Candidate', short: 'Weak' },
  2: { full: 'Stage 2 — Research Candidate', short: 'Research' },
  3: { full: 'Stage 3 — Promotion Candidate', short: 'Promotion' },
};

const STAGE_DESCRIPTIONS: Record<0 | 1 | 2 | 3, string> = {
  0: 'Failed one or more Stage 0 kill criteria. Downstream metrics are not actionable.',
  1: 'Survived the Stage 0 kill switch. Sharpe CI and walk-forward details are enabled.',
  2: 'Survives walk-forward and parameter sensitivity. Bootstrap CI and cross-asset enabled.',
  3: 'Strong evidence across folds and configurations. Promotion-stage gates apply.',
};

@Component({
  selector: 'app-graduation-ladder',
  standalone: true,
  imports: [CommonModule, TooltipModule],
  templateUrl: './graduation-ladder.component.html',
  styleUrls: ['./graduation-ladder.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class GraduationLadderComponent {
  readonly stageInfo = input.required<GraduationStageInfo>();
  readonly stage0Rejection = input<Stage0Rejection | null>(null);

  readonly nodes = computed<LadderNode[]>(() => {
    const current = this.stageInfo().stage;
    return ([0, 1, 2, 3] as const).map((s) => {
      const labels = STAGE_LABELS[s];
      let state: LadderNode['state'];
      if (s === current) {
        state = 'current';
      } else if (s < current) {
        state = 'passed';
      } else if (s === current + 1) {
        state = 'next';
      } else {
        state = 'future';
      }
      return {
        stage: s,
        label: labels.full,
        shortLabel: labels.short,
        state,
        tooltip: STAGE_DESCRIPTIONS[s],
      };
    });
  });

  /** Advance criteria for the *next* stage from the current one. Returned in
   *  display order so the template can iterate without sorting. */
  readonly advanceCriteria = computed<StageAdvanceCriterion[]>(
    () => this.stageInfo().advanceCriteria ?? [],
  );

  readonly hasNextStage = computed(() => this.stageInfo().nextStageLabel.length > 0);
  readonly atTop = computed(() => this.stageInfo().stage === 3);
  readonly atBottom = computed(() => this.stageInfo().stage === 0);

  /** A single node's visual band. Mirrors the engine-lab readiness-score-card
   *  semantics (green / amber / red / na) so the page reads consistent with
   *  Engine Lab. */
  bandFor(node: LadderNode): 'green' | 'amber' | 'red' | 'na' {
    if (node.state === 'future' || node.state === 'next') return 'na';
    if (node.stage === 0) return 'red';
    if (node.stage === 1) return 'amber';
    return 'green';
  }
}
