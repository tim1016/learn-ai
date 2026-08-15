import { ChangeDetectionStrategy, Component } from '@angular/core';

interface ThresholdCandidate {
  readonly value: number;
  readonly controlNeighbourhood: boolean;
}

@Component({
  selector: 'app-spy-ema-protocol',
  templateUrl: './spy-ema-protocol.component.html',
  styleUrl: './spy-ema-protocol.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SpyEmaProtocolComponent {
  readonly folds = Array.from({ length: 18 }, (_, index) => index + 1);
  readonly thresholds: readonly ThresholdCandidate[] = [1, 2, 3, 4, 5, 7.5, 10]
    .map((value) => ({ value, controlNeighbourhood: value === 4 }));
}
