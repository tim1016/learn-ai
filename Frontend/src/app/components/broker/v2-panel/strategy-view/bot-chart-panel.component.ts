import { NgTemplateOutlet } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  TemplateRef,
  computed,
  input,
  linkedSignal,
  model,
  output,
  signal,
  viewChildren,
} from '@angular/core';

import { AssetIdentityComponent } from '../../../../shared/asset-identity/asset-identity.component';
import type { StrategyViewResponse } from '../lib/broker-v2-panel.types';
import { StrategyGatePickerComponent } from './strategy-gate-picker.component';
import { readGatePreference, writeGatePreference } from './strategy-gate-preference';
import { StrategyIndicatorsComponent } from './strategy-indicators.component';
import { StrategyViewComponent } from './strategy-view.component';
import {
  decisionTimeframeLabel,
  resolveActiveGate,
  type StrategyViewFailure,
} from './strategy-view-model';

export type BotChartTab = 'strategy' | 'tape';

let nextChartPanelId = 0;

/**
 * A bot's chart panel (#2639): the strategy's own decision candles by
 * default, and the market tape one tab away.
 *
 * Self-contained so it can move into the bot page's board unchanged: the host
 * hands it the strategy view read, the read's state and a template for the
 * tape, and shares candle selection two-way with whatever lists the same
 * decisions. The panel owns the tab, the gate the viewer picked (remembered
 * per strategy in this browser) and the header chips. Both tabs keep their
 * content once created, so zoom, scroll and an open popover survive a trip
 * to the other tab; the tape is created the first time its tab opens.
 */
@Component({
  selector: 'app-bot-chart-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AssetIdentityComponent,
    NgTemplateOutlet,
    StrategyGatePickerComponent,
    StrategyIndicatorsComponent,
    StrategyViewComponent,
  ],
  templateUrl: './bot-chart-panel.component.html',
  styleUrl: './bot-chart-panel.component.scss',
})
export class BotChartPanelComponent {
  readonly symbol = input.required<string>();
  readonly view = input<StrategyViewResponse | null>(null);
  readonly loading = input(false);
  readonly failure = input<StrategyViewFailure | null>(null);
  /** The market tape, created when its tab first opens. */
  readonly tape = input.required<TemplateRef<unknown>>();
  readonly selectedBarCloseMs = model<number | null>(null);

  readonly retry = output();

  protected readonly ids = (() => {
    const base = `bot-chart-panel-${nextChartPanelId++}`;
    return {
      strategyTab: `${base}-strategy-tab`,
      strategyPanel: `${base}-strategy-panel`,
      tapeTab: `${base}-tape-tab`,
      tapePanel: `${base}-tape-panel`,
    };
  })();

  protected readonly activeTab = signal<BotChartTab>('strategy');
  protected readonly tapeOpened = signal(false);
  private readonly tabs = viewChildren<ElementRef<HTMLButtonElement>>('tab');

  protected readonly declaration = computed(() => this.view()?.declaration ?? null);

  protected readonly strategyTabLabel = computed(() => {
    const view = this.view();
    return view === null ? 'Strategy' : `Strategy · ${decisionTimeframeLabel(view.decision_timeframe_ms)}`;
  });

  /** The viewer's pick for this strategy, reset when the strategy changes. */
  private readonly chosenGateId = linkedSignal({
    source: () => this.view()?.strategy_key ?? null,
    computation: (strategyKey: string | null) => (strategyKey === null ? null : readGatePreference(strategyKey)),
  });

  protected readonly activeGate = computed(() => {
    const declaration = this.declaration();
    return declaration === null ? null : resolveActiveGate(declaration, this.chosenGateId());
  });

  protected selectTab(tab: BotChartTab): void {
    this.activeTab.set(tab);
    if (tab === 'tape') this.tapeOpened.set(true);
  }

  protected onTabKeydown(event: KeyboardEvent): void {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const tab: BotChartTab = event.key === 'ArrowLeft' || event.key === 'Home' ? 'strategy' : 'tape';
    this.selectTab(tab);
    this.tabs().find((each) => each.nativeElement.dataset['chartTab'] === tab)?.nativeElement.focus();
  }

  protected chooseGate(gateId: string): void {
    this.chosenGateId.set(gateId);
    const strategyKey = this.view()?.strategy_key;
    if (strategyKey !== undefined) writeGatePreference(strategyKey, gateId);
  }
}
