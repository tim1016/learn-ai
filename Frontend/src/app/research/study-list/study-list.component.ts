import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterLink } from '@angular/router';

import { formatReceiptLabel } from '../../shared/pipes/receipt-label.pipe';
import { WorkspaceHeaderComponent } from '../../shared/workspace/workspace-header.component';
import { menuLabelFor } from '../../shell/app-menu';
import {
  RESEARCH_DEMOS,
  RESEARCH_HISTORY,
  RESEARCH_STUDIES,
  RULE_SANDBOX,
} from '../research-studies';

const PURPOSE =
  'Each study asks one question of an instrument, or of a universe of them, and says what it could not tell you.';

/**
 * Research opens here (ADR 0084): every study with the question it answers and
 * where its data comes from today, the Rule sandbox, the Edge demonstrations
 * (sample data, never measured) and the History of every saved result.
 *
 * Presentation only; opening this page runs nothing.
 */
@Component({
  selector: 'app-study-list',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, WorkspaceHeaderComponent],
  templateUrl: './study-list.component.html',
  styleUrl: './study-list.component.scss',
})
export class StudyListComponent {
  protected readonly purpose = PURPOSE;

  protected readonly studies = RESEARCH_STUDIES.map((study) => ({
    id: study.id,
    route: study.route,
    title: menuLabelFor(study.route),
    question: study.question,
    reads: study.source.sources.map(formatReceiptLabel).join(', '),
  }));
  protected readonly ruleSandbox = { ...RULE_SANDBOX, title: menuLabelFor(RULE_SANDBOX.route) };
  protected readonly demos = RESEARCH_DEMOS.map((demo) => ({
    id: demo.id,
    route: demo.route,
    title: menuLabelFor(demo.route),
    summary: demo.summary,
  }));
  protected readonly history = { ...RESEARCH_HISTORY, title: menuLabelFor(RESEARCH_HISTORY.route) };
}
