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
import type { CustomGateInput, StrategyViewResponse } from '../lib/broker-v2-panel.types';
import type { StrategyRunContext } from './chart-lanes';
import { strategyCatalogueIndicators } from './strategy-catalogue-indicators';
import { StrategyGatePickerComponent } from './strategy-gate-picker.component';
import { readGatePreference, writeGatePreference } from './strategy-gate-preference';
import { DRAFT_GATE_ID, gateVariableGroups } from './strategy-gates-model';
import { strategyGatesState } from './strategy-gates-state';
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
 * content once created, so each chart's zoom and scroll survive a trip to
 * the other tab; the tape is created the first time its tab opens.
 *
 * The panel also brings the strategy's custom gates (saved on the data
 * plane, judged there on these candles) and the catalogue indicators the
 * viewer adds, so a host only supplies the read.
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
  /** The second tab's name: the bot page's market tape, or Strategy Lab's price-and-trades chart. */
  readonly tapeLabel = input('Tape');
  readonly selectedBarCloseMs = model<number | null>(null);
  /** The bot page's run facts for the strategy chart's lanes, Now line and forming bar (#2794). */
  readonly runContext = input<StrategyRunContext | null>(null);

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

  protected readonly gates = strategyGatesState(this.view);
  protected readonly indicators = strategyCatalogueIndicators(this.view);
  /** The read, with the strategy's custom gates folded in. */
  protected readonly shownView = this.gates.view;
  protected readonly declaration = computed(() => this.shownView()?.declaration ?? null);
  protected readonly gateVariables = computed(() => {
    const view = this.shownView();
    return view === null ? [] : gateVariableGroups(view, this.gates.catalogue());
  });

  protected readonly strategyTabLabel = computed(() => {
    const view = this.view();
    return view === null ? 'Strategy' : `Strategy · ${decisionTimeframeLabel(view.decision_timeframe_ms)}`;
  });

  /** The viewer's pick for this strategy, reset when the strategy changes. A
   * re-read of the same strategy reruns the computation too, and keeps the
   * pick: storage may be blocked, so it is not re-read from there. */
  private readonly chosenGateId = linkedSignal<string | null, string | null>({
    source: () => this.view()?.strategy_key ?? null,
    computation: (strategyKey, previous) => {
      if (previous !== undefined && previous.source === strategyKey) return previous.value;
      return strategyKey === null ? null : readGatePreference(strategyKey);
    },
  });

  /** A previewed draft shades the chart until the editor closes. */
  protected readonly activeGate = computed(() => {
    const declaration = this.declaration();
    if (declaration === null) return null;
    const draft = this.gates.previewing() ? declaration.gates.find((gate) => gate.gate_id === DRAFT_GATE_ID) : undefined;
    return draft ?? resolveActiveGate(declaration, this.chosenGateId());
  });

  /** Saves a gate and makes it the one shading the chart. */
  protected readonly saveGate = async (draft: CustomGateInput, gateId: string | null): Promise<void> => {
    const gate = await this.gates.save(draft, gateId);
    this.chooseGate(gate.gate_id);
  };
  protected readonly removeGate = (gateId: string): Promise<void> => this.gates.remove(gateId);

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
