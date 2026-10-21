import { ChangeDetectionStrategy, Component, computed, inject, input, linkedSignal, resource } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { components } from '../../api/broker.types';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import { OptionsGoldenSearchService, optionsSearchErrorMessage } from './options-golden-search.service';
import type { EvidenceWindow } from './options-golden-search.types';

type FailureEvent = components['schemas']['OptionsFailureEvent'];

@Component({
  selector: 'app-options-failure-evidence',
  imports: [RouterLink, ReceiptLabelPipe, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-failure-evidence.component.html',
  styleUrl: './options-golden-search.component.scss',
})
export class OptionsFailureEvidenceComponent {
  private readonly api = inject(OptionsGoldenSearchService);
  readonly studyId = input.required<string>();
  readonly digest = input.required<string>();
  readonly window = input<EvidenceWindow>('development');
  private readonly identity = computed(() => `${this.studyId()}:${this.digest()}:${this.window()}`);
  protected readonly offset = linkedSignal({ source: this.identity, computation: () => 0 });
  protected readonly target = linkedSignal<string, FailureEvent | null>({ source: () => `${this.identity()}:${this.offset()}`, computation: () => null });
  protected readonly failures = resource({ params: () => ({ identity: this.identity(), study: this.studyId(), digest: this.digest(), scope: this.window(), offset: this.offset() }),
    loader: ({ params }) => this.api.failurePage(params.study, params.digest, params.scope, params.offset) });
  protected readonly inspection = resource({ params: () => {
    const target = this.target();
    return target === null ? undefined : { identity: this.identity(), study: this.studyId(), digest: this.digest(), scope: this.window(), target };
  }, loader: ({ params }) => this.api.inspectFailure(params.study, params.digest, params.scope, params.target) });
  protected readonly problem = computed(() => this.failures.error() === undefined ? null
    : optionsSearchErrorMessage(this.failures.error(), 'The retained failure evidence could not be read. No replay or capture was attempted.'));
  protected readonly inspectionProblem = computed(() => this.inspection.error() === undefined ? null
    : optionsSearchErrorMessage(this.inspection.error(), 'The selected retained input could not be inspected. Its cause remains unknown.'));
}
