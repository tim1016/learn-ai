import {
  ChangeDetectionStrategy,
  Component,
  Injectable,
  Injector,
  computed,
  inject,
  input,
  signal,
  untracked,
  type Signal,
  type WritableSignal,
} from '@angular/core';

import { GoldenOptionSetupsService } from '../../components/options-backtest/golden-option-setups.service';

interface LabelledBot {
  readonly strategy_label: string | null;
  readonly options_bot?: { readonly golden_setup_id: string } | null;
}

/** How long a setup whose name could not be read waits before it is asked again. */
export const SETUP_NAME_RETRY_MS = 60_000;

/**
 * Approved setups' names, read once per id. A golden record is insert-only,
 * so its name never changes; a setup approved after the page opened is read
 * the first time a bot names it.
 */
@Injectable({ providedIn: 'root' })
export class GoldenSetupNames {
  private readonly golden = inject(GoldenOptionSetupsService);
  private readonly names = new Map<string, WritableSignal<string | null>>();
  /** When each unreadable setup's last read failed (wall-clock ms, local throttling only). */
  private readonly failedAt = new Map<string, number>();

  /** The setup's name: null until it is read, and while it cannot be. */
  nameFor(goldenSetupId: string): Signal<string | null> {
    const known = this.names.get(goldenSetupId);
    const failed = this.failedAt.get(goldenSetupId);
    if (known !== undefined && (failed === undefined || Date.now() - failed < SETUP_NAME_RETRY_MS)) return known;
    const name = known ?? signal<string | null>(null);
    this.names.set(goldenSetupId, name);
    this.failedAt.delete(goldenSetupId);
    this.golden.get(goldenSetupId).then(
      (setup) => name.set(setup.name),
      // The label keeps the bot's strategy label; a roster poll after the
      // retry interval asks again.
      () => this.failedAt.set(goldenSetupId, Date.now()),
    );
    return name;
  }
}

/**
 * What a bot runs: an Options bot's approved setup by name, else the
 * backend's strategy label. Bots of one strategy on one symbol otherwise read
 * the same in every row.
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
    // The names service, and the read it starts, are reached only for an
    // Options bot, and untracked: starting a read is not this label's state.
    const name = golden ? untracked(() => this.injector.get(GoldenSetupNames).nameFor(golden)) : null;
    return name?.() || bot.strategy_label || '';
  });
}
