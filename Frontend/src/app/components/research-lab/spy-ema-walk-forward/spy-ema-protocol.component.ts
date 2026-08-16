import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
} from '@angular/core';
import { CardModule } from 'primeng/card';
import { MessageModule } from 'primeng/message';

import type {
  WalkForwardConfig,
  WalkForwardResult,
} from '../../../services/walk-forward.types';

@Component({
  selector: 'app-spy-ema-protocol',
  imports: [CardModule, MessageModule],
  templateUrl: './spy-ema-protocol.component.html',
  styleUrl: './spy-ema-protocol.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SpyEmaProtocolComponent {
  readonly config = input<WalkForwardConfig | null>(null);
  readonly result = input<WalkForwardResult | null>(null);

  readonly rollingPolicy = computed(() => {
    const policy = this.config()?.split_policy;
    return policy?.kind === 'rolling' ? policy : null;
  });

  readonly thresholds = computed(() => (
    this.config()?.parameter_search?.candidates
      .map((candidate) => candidate.parameters['gap_bps'])
      .filter((value): value is number => Number.isFinite(value))
    ?? []
  ));

  readonly folds = computed(() => this.result()?.folds ?? []);
}
