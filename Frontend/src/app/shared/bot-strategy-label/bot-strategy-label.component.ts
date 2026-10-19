import { ChangeDetectionStrategy, Component, Injectable, Injector, computed, inject, input, resource, untracked } from '@angular/core';

import { GoldenOptionSetupsService } from '../../components/options-backtest/golden-option-setups.service';

interface LabelledBot {
  readonly strategy_label?: string | null;
  readonly options_bot?: { readonly golden_setup_id: string } | null;
}

/** Approved setups' names, read once per page; a record's name never changes. */
@Injectable({ providedIn: 'root' })
export class GoldenSetupNames {
  private readonly golden = inject(GoldenOptionSetupsService);
  private readonly setups = resource({ loader: () => this.golden.list() });

  /** The approved setup's name, or null while unread, unreadable or unlisted. */
  nameFor(goldenSetupId: string): string | null {
    if (!this.setups.hasValue()) return null;
    return this.setups.value().find((setup) => setup.id === goldenSetupId)?.name ?? null;
  }
}

/**
 * What a bot runs: an Options bot's approved setup by name, else the
 * backend's strategy label. Bots of one strategy on one symbol otherwise read
 * the same in every row. The names are read only once an Options bot shows.
 */
@Component({
  selector: 'app-bot-strategy-label',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `{{ label() }}`,
})
export class BotStrategyLabelComponent {
  readonly bot = input.required<LabelledBot>();

  private readonly injector = inject(Injector);

  protected readonly label = computed(() => {
    const bot = this.bot();
    const golden = bot.options_bot?.golden_setup_id;
    const names = golden ? untracked(() => this.injector.get(GoldenSetupNames)) : null;
    return (golden && names?.nameFor(golden)) || bot.strategy_label || '';
  });
}
