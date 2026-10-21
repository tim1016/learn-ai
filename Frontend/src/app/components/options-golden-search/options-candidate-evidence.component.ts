import { ChangeDetectionStrategy, Component, computed, input, linkedSignal, resource } from '@angular/core';

import type { components } from '../../api/broker.types';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import { OptionsSearchCandidateComponent } from './options-search-candidate.component';
import type { EvidenceWindow, SessionLoader } from './options-golden-search.types';
import { optionsSearchErrorMessage } from './options-golden-search.service';

export type CandidateEvidence = components['schemas']['OptionsCandidateEvidence'];
export type CandidatePage = components['schemas']['OptionsCandidatePage'];
export type CandidateLoader = (study: string, digest: string, window: EvidenceWindow, offset: number) => Promise<CandidateEvidence>;
export type CandidatePageLoader = (study: string, offset: number) => Promise<CandidatePage>;

@Component({
  selector: 'app-options-candidate-evidence',
  imports: [OptionsSearchCandidateComponent, ReceiptLabelPipe, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-candidate-evidence.component.html',
  styleUrl: './options-golden-search.component.scss',
})
export class OptionsCandidateEvidenceComponent {
  readonly studyId = input.required<string>();
  readonly digest = input.required<string>();
  readonly window = input<EvidenceWindow>('development');
  readonly loader = input.required<CandidateLoader>();
  readonly sessionLoader = input<SessionLoader | null>(null);
  private readonly identity = computed(() => `${this.studyId()}:${this.digest()}:${this.window()}`);
  protected readonly offset = linkedSignal({ source: this.identity, computation: () => 0 });
  protected readonly evidence = resource({ params: () => ({ identity: this.identity(), study: this.studyId(), digest: this.digest(), window: this.window(), offset: this.offset() }),
    loader: ({ params }) => this.loader()(params.study, params.digest, params.window, params.offset) });
  protected readonly candidate = computed(() => {
    if (!this.evidence.hasValue()) return null;
    const value = this.evidence.value().candidate;
    return { ...value, sessions: value.sessions.map(session => ({ ...session, attempts: session.attempts ?? [] })) };
  });
  protected readonly problem = computed(() => this.evidence.error() === undefined ? null
    : optionsSearchErrorMessage(this.evidence.error(), 'This retained candidate artifact is unavailable. Retry the exact evidence; a new replay cannot replace it.'));
}
