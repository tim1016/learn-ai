import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import { ExperimentalNoticeComponent } from '../../shared/experimental-notice/experimental-notice.component';
import { ALWAYS_READY } from '../read-state';
import type { StrategyRegistryEntry } from '../signal-strategy-evidence';
import { SignalStrategySectionComponent } from '../signal-strategy-section.component';

/**
 * What the registry itself says the strategy is: the entry's own description
 * and the program's own statement of its rules. Where the registry holds no
 * words, the page says so; it never writes a description from the strategy's name.
 */
@Component({
  selector: 'app-signal-strategy-description',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ExperimentalNoticeComponent, SignalStrategySectionComponent],
  templateUrl: './strategy-description.component.html',
  styleUrl: '../section-content.scss',
})
export class SignalStrategyDescriptionComponent {
  readonly entry = input.required<StrategyRegistryEntry>();
  readonly viewSource = output();

  protected readonly declared = ALWAYS_READY;

  protected readonly paragraphs = computed(() =>
    this.entry()
      .description.split(/\n\s*\n/)
      .map((paragraph) => paragraph.trim())
      .filter((paragraph) => paragraph !== ''),
  );
}
