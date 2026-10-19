import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import { etIsoDate } from '../../../shared/date/et-midnight';
import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { OptionsQualificationEvidenceComponent } from '../../options-backtest/options-qualification-evidence.component';
import { PositionPreviewComponent } from '../../options-backtest/position/position-preview.component';
import { SetupSentencesComponent } from '../../options-backtest/cards/setup-sentences.component';
import type { SetupSummary } from '../../options-backtest/options-backtest.types';
import type { GoldenOptionSetup } from '../../options-backtest/golden-option-setups.service';

/** Read-only evidence and payoff preview for the immutable version selected at Deploy. */
@Component({
  selector: 'app-options-bot-setup-desk',
  imports: [AuthoredUsdPipe, OptionsQualificationEvidenceComponent, PositionPreviewComponent, SetupSentencesComponent, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-bot-setup-desk.component.html',
  styleUrl: './options-bot-setup-desk.component.scss',
})
export class OptionsBotSetupDeskComponent {
  readonly golden = input.required<GoldenOptionSetup>();
  readonly summary = input<SetupSummary | null>(null);
  readonly summaryFailed = input(false);
  readonly retrySummary = output();
  protected readonly previewDate = computed(() => etIsoDate(this.golden().evidence.last_session_open_ms));
}
