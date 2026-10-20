import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { OptionContractComponent } from '../../../../shared/option-contract/option-contract.component';
import { optionContractFromSymbol } from '../../../../shared/option-contract/option-contract-symbol';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import type { BotPanelView } from '../lib/broker-v2-panel.types';

@Component({
  selector: 'app-bot-option-legs',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [OptionContractComponent, ReceiptLabelPipe],
  templateUrl: './bot-option-legs.component.html',
  styleUrl: './bot-option-legs.component.scss',
})
export class BotOptionLegsComponent {
  readonly traded = input.required<NonNullable<BotPanelView['traded']>>();

  protected readonly rows = computed(() => (this.traded().legs ?? []).map(leg => ({
    leg, contract: optionContractFromSymbol(leg.symbol, this.traded().symbol),
  })));
}
