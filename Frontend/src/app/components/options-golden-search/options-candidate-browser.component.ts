import { ChangeDetectionStrategy, Component, computed, input, linkedSignal, resource } from '@angular/core';

import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { OptionsCandidateEvidenceComponent, type CandidateLoader, type CandidatePageLoader } from './options-candidate-evidence.component';
import type { SessionLoader } from './options-golden-search.types';
import { optionsSearchErrorMessage } from './options-golden-search.service';

@Component({
  selector: 'app-options-candidate-browser',
  imports: [ReceiptLabelPipe, OptionsCandidateEvidenceComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-candidate-browser.component.html',
  styleUrl: './options-golden-search.component.scss',
})
export class OptionsCandidateBrowserComponent {
  readonly studyId = input.required<string>();
  readonly pageLoader = input.required<CandidatePageLoader>();
  readonly candidateLoader = input.required<CandidateLoader>();
  readonly sessionLoader = input<SessionLoader | null>(null);
  protected readonly offset = linkedSignal({ source: this.studyId, computation: () => 0 });
  protected readonly selected = linkedSignal<string, string | null>({ source: this.studyId, computation: () => null });
  protected readonly page = resource({ params: () => ({ study: this.studyId(), offset: this.offset() }), loader: ({ params }) => this.pageLoader()(params.study, params.offset) });
  protected readonly problem = computed(() => this.page.error() === undefined ? null : optionsSearchErrorMessage(this.page.error(), 'Candidate outcomes could not be read. Retry the retained list.'));
}
