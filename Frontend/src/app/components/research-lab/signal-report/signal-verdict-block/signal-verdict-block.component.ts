import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { TooltipModule } from 'primeng/tooltip';
import { TagModule } from 'primeng/tag';
import {
  GraduationResult,
  SharpeCi,
  SignalEngineResult,
} from '../../../../services/research.service';
import { GraduationLadderComponent } from '../graduation-ladder/graduation-ladder.component';

@Component({
  selector: 'app-signal-verdict-block',
  standalone: true,
  imports: [CommonModule, TooltipModule, TagModule, GraduationLadderComponent],
  templateUrl: './signal-verdict-block.component.html',
  styleUrls: ['./signal-verdict-block.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SignalVerdictBlockComponent {
  readonly result = input.required<SignalEngineResult>();

  readonly graduation = computed<GraduationResult | null>(() => this.result().graduation);

  readonly stage = computed<0 | 1 | 2 | 3>(() => {
    return (this.graduation()?.stageInfo?.stage ?? 0) as 0 | 1 | 2 | 3;
  });

  readonly stageLabel = computed<string>(
    () => this.graduation()?.stageInfo?.label ?? 'Rejected',
  );

  readonly stageDescription = computed<string>(
    () => this.graduation()?.stageInfo?.description ?? '',
  );

  /** Visual band shared with the engine-lab readiness-score-card so the
   *  page reads consistent across the research-lab and engine-lab. */
  readonly band = computed<'green' | 'amber' | 'red' | 'na'>(() => {
    switch (this.stage()) {
      case 3:
        return 'green';
      case 2:
        return 'green';
      case 1:
        return 'amber';
      case 0:
        return 'red';
      default:
        return 'na';
    }
  });

  readonly featureLabel = computed<string>(() => {
    const r = this.result();
    return `${r.featureName} on ${r.ticker.toUpperCase()}`;
  });

  readonly dateRange = computed<string>(() => {
    const r = this.result();
    return `${r.startDate} → ${r.endDate}`;
  });

  readonly sharpeCi = computed<SharpeCi | null>(() => this.result().oosSharpeCi);

  readonly hasSharpe = computed<boolean>(() => {
    const wf = this.result().walkForward;
    return wf !== null && wf.windows.length > 0;
  });

  readonly headlineSharpe = computed<number>(
    () => this.result().walkForward?.meanOosSharpe ?? 0,
  );

  /** "0.27 ± 0.18" or just "0.27" when the CI was not computed. */
  readonly sharpeDisplay = computed<string>(() => {
    const sharpe = this.headlineSharpe();
    const ci = this.sharpeCi();
    if (ci?.valid) {
      const halfWidth = (ci.ciUpper - ci.ciLower) / 2;
      return `${sharpe.toFixed(2)} ± ${halfWidth.toFixed(2)}`;
    }
    return sharpe.toFixed(2);
  });

  readonly ciIntervalDisplay = computed<string | null>(() => {
    const ci = this.sharpeCi();
    if (!ci?.valid) return null;
    return `95% CI [${ci.ciLower.toFixed(2)}, ${ci.ciUpper.toFixed(2)}]`;
  });

  readonly ciStraddlesZero = computed<boolean>(() => {
    const ci = this.sharpeCi();
    if (!ci?.valid) return false;
    return ci.ciLower < 0 && ci.ciUpper > 0;
  });

  /** A one-sentence verdict written in plain English for a graduate-level
   *  reader who knows what Sharpe means but not the deeper machinery. */
  readonly verdictText = computed<string>(() => {
    const g = this.graduation();
    const stage = this.stage();
    if (!g) return 'No graduation result available.';
    if (stage === 0) {
      const failedNames = g.stage0Rejection?.failedCriteria?.map((f) => f.criterionName) ?? [];
      const list =
        failedNames.length === 0
          ? ''
          : ` (${failedNames.slice(0, 2).join(', ')}${failedNames.length > 2 ? `, +${failedNames.length - 2} more` : ''})`;
      return `This signal failed the Stage 0 kill switch${list}. Further inspection is unlikely to change the conclusion — try a different feature, horizon, or regime gate.`;
    }
    if (stage === 1) {
      return 'This signal survived the Stage 0 kill switch but does not yet show enough evidence to warrant cross-asset validation. Headline statistics below.';
    }
    if (stage === 2) {
      return 'This signal survives walk-forward and parameter-sensitivity gates. Block-bootstrap CIs and cross-asset validation are appropriate next steps.';
    }
    return 'This signal meets all promotion-stage criteria on the validated metrics. Deflated Sharpe gating and capacity modelling apply before live deployment.';
  });

  /** Suggested next action for the reader, in plain English. */
  readonly nextActionText = computed<string>(() => {
    switch (this.stage()) {
      case 0:
        return 'Pick a different feature or change the horizon — the diagnostic detail below is for audit only.';
      case 1:
        return 'Inspect walk-forward folds and parameter stability. Tighten the regime gate or extend the date range to gather more independent observations.';
      case 2:
        return 'Run cross-sectional validation on related tickers and rerun with block-bootstrap CIs.';
      case 3:
        return 'Compute Deflated Sharpe on the in-sample grid headline and run capacity / impact modelling before paper-trading.';
      default:
        return '';
    }
  });

  readonly gradeChipBand = computed<'green' | 'amber' | 'red' | 'na'>(() => {
    const grade = this.graduation()?.overallGrade ?? 'F';
    if (grade === 'A' || grade === 'B') return 'green';
    if (grade === 'C') return 'amber';
    return 'red';
  });
}
