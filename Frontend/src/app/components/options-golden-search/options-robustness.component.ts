import { DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, inject, input, linkedSignal, output, resource } from '@angular/core';

import { AuthoredUsdPipe } from '../../shared/pipes/authored-usd.pipe';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import type { EvidenceWindow } from './options-golden-search.types';
import { optionsSearchErrorMessage } from './options-golden-search.service';
import { OptionsRobustnessService } from './options-robustness.service';

@Component({
  selector: 'app-options-robustness',
  imports: [DecimalPipe, AuthoredUsdPipe, ReceiptLabelPipe, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-robustness.component.html',
  styleUrl: './options-robustness.component.scss',
})
export class OptionsRobustnessComponent {
  readonly studyId = input.required<string>();
  readonly digest = input.required<string>();
  readonly window = input<EvidenceWindow>('development');
  readonly canInspectNeighbors = input(false);
  readonly inspectNeighbor = output<string>();
  private readonly service = inject(OptionsRobustnessService);
  private readonly identity = computed(() => ({ study: this.studyId(), digest: this.digest(), window: this.window() }));
  protected readonly offset = linkedSignal({ source: this.identity, computation: () => 0 });
  protected readonly read = resource({ params: this.identity, loader: ({ params }) => this.service.read(params.study, params.digest, params.window) });
  protected readonly neighbors = resource({ params: () => ({ ...this.identity(), offset: this.offset() }),
    loader: ({ params }) => this.service.neighbors(params.study, params.digest, params.window, params.offset) });
  protected readonly problem = computed(() => this.read.error() === undefined ? null
    : optionsSearchErrorMessage(this.read.error(), 'Retained robustness evidence is unavailable. Retry its exact artifact.'));
  protected readonly neighborProblem = computed(() => this.neighbors.error() === undefined ? null
    : optionsSearchErrorMessage(this.neighbors.error(), 'Declared neighbor evidence could not be read.'));
}
