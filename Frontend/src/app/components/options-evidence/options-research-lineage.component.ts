import { ChangeDetectionStrategy, Component, computed, inject, input, linkedSignal, resource } from '@angular/core';
import { RouterLink } from '@angular/router';

import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import { optionsStudyRoute } from '../../options-setups/options-setups.address';
import { extractServerMessage } from '../broker/operation-error';
import { OptionsResearchLineageService } from './options-research-lineage.service';
import { OptionsResearchTrialsComponent } from './options-research-trials.component';

@Component({
  selector: 'app-options-research-lineage',
  imports: [RouterLink, ReceiptLabelPipe, TimestampDisplayComponent, OptionsResearchTrialsComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-research-lineage.component.html',
  styleUrl: './options-research-lineage.component.scss',
})
export class OptionsResearchLineageComponent {
  readonly studyId = input.required<string>();
  protected readonly studyRoute = optionsStudyRoute;
  private readonly service = inject(OptionsResearchLineageService);
  protected readonly evidence = resource({ params: () => this.studyId(), loader: ({ params }) => this.service.read(params) });
  protected readonly trialsOpen = linkedSignal({ source: this.studyId, computation: () => false });
  protected readonly problem = computed(() => this.evidence.error() === undefined ? null : extractServerMessage(this.evidence.error(), 'This frozen research procedure could not be read.'));

  protected showTrials(event: Event): void {
    if (event.target instanceof HTMLDetailsElement) this.trialsOpen.set(event.target.open);
  }
}
