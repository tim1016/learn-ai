import { ChangeDetectionStrategy, Component, computed, signal } from '@angular/core';

import {
  IndicatorPickerAdd,
  IndicatorPickerComponent,
  IndicatorPickerPreview,
} from '../../shared/indicator-picker/indicator-picker.component';
import { IndicatorCategory } from '../../shared/indicator-catalog/indicator-catalog.service';

// Layout-regression sandbox for the .ide-grid primitive. Hosts three rails
// of dummy content tall enough to exercise sticky/scroll behavior, plus a
// live <app-indicator-picker> in the right rail so we can eyeball the picker
// at all three layout breakpoints.
@Component({
  selector: 'app-ide-sandbox',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [IndicatorPickerComponent],
  template: `
    <header class="sandbox-header">
      <h2>IDE layout sandbox</h2>
      <p class="muted">
        Resize the window. At &lt;1100px content width everything stacks.
        At ≥1100 the left rail and main go sticky; the right rail reflows
        beneath main. At ≥1500 the right rail promotes to a third sticky
        column. The right rail hosts the new indicator picker for
        visual verification.
      </p>
    </header>

    <div class="ide-grid" data-testid="ide-grid">
      <aside class="ide-rail-left" data-testid="ide-rail-left">
        @for (i of tall; track i) {
          <div class="tile tile--left">Left rail tile {{ i }}</div>
        }
      </aside>

      <section class="ide-main" data-testid="ide-main">
        <div class="tile tile--main">
          Last picker event: <code class="mono">{{ lastEvent() }}</code>
        </div>
        @for (i of tall; track i) {
          <div class="tile tile--main">Main workspace tile {{ i }}</div>
        }
      </section>

      <aside class="ide-rail-right" data-testid="ide-rail-right">
        <div class="picker-host">
          <app-indicator-picker
            [categories]="catalog()"
            [activeKeys]="activeKeys()"
            (add)="onAdd($event)"
            (addInstance)="onAddInstance($event)"
            (preview)="onPreview($event)" />
        </div>
      </aside>
    </div>
  `,
  styles: [`
    :host { display: block; }

    .sandbox-header {
      margin-bottom: 1rem;
    }

    .tile {
      background: var(--bg-surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 1rem;
      min-height: 6rem;
    }

    .tile--left  { border-left:  3px solid var(--accent); }
    .tile--main  { border-left:  3px solid var(--bull); }
    .tile--right { border-left:  3px solid var(--bear); }

    .picker-host {
      background: var(--bg-surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 1rem;
    }

    .mono { font-family: var(--font-mono); }
  `],
})
export class IdeSandboxComponent {
  // 24 dummy tiles per rail — enough to force overflow at every breakpoint
  // so sticky/scroll behavior is observable.
  protected readonly tall = Array.from({ length: 24 }, (_, i) => i + 1);

  // Static catalog stub so the picker renders without a backend call.
  protected readonly catalog = signal<IndicatorCategory[]>([
    {
      name: 'trend',
      indicators: [
        { name: 'ema',  category: 'trend', description: 'Exponential moving average.',  configurable_params: [{ name: 'length', type: 'int', default: 10, min: 2, max: 200, description: 'Window length.' }] },
        { name: 'sma',  category: 'trend', description: 'Simple moving average.',       configurable_params: [{ name: 'length', type: 'int', default: 10, min: 2, max: 200, description: 'Window length.' }] },
        { name: 'vwap', category: 'trend', description: 'Volume-weighted average price.', configurable_params: [] },
        { name: 'adx',  category: 'trend', description: 'Average directional index.',   configurable_params: [{ name: 'length', type: 'int', default: 14, min: 2, max: 200, description: 'Window length.' }] },
        { name: 'supertrend', category: 'trend', description: 'ATR-banded trend regime.', configurable_params: [{ name: 'length', type: 'int', default: 10, min: 2, max: 200, description: 'Window length.' }] },
      ],
    },
    {
      name: 'momentum',
      indicators: [
        { name: 'rsi',   category: 'momentum', description: 'Relative strength index.', configurable_params: [{ name: 'length', type: 'int', default: 14, min: 2, max: 200, description: 'Window length.' }] },
        { name: 'macd',  category: 'momentum', description: 'MACD line + signal + histogram.', configurable_params: [] },
        { name: 'stoch', category: 'momentum', description: 'Stochastic %K / %D.',     configurable_params: [{ name: 'k', type: 'int', default: 14, min: 2, max: 200, description: 'k length.' }] },
        { name: 'cci',   category: 'momentum', description: 'Commodity channel index.', configurable_params: [{ name: 'length', type: 'int', default: 20, min: 2, max: 200, description: 'Window length.' }] },
      ],
    },
    {
      name: 'volatility',
      indicators: [
        { name: 'bbands',   category: 'volatility', description: 'Bollinger bands.',  configurable_params: [{ name: 'length', type: 'int', default: 20, min: 2, max: 200, description: 'Window length.' }] },
        { name: 'atr',      category: 'volatility', description: 'Average true range.', configurable_params: [{ name: 'length', type: 'int', default: 14, min: 2, max: 200, description: 'Window length.' }] },
        { name: 'keltner',  category: 'volatility', description: 'Keltner channel — EMA ± k·ATR.', configurable_params: [{ name: 'length', type: 'int', default: 20, min: 2, max: 200, description: 'Window length.' }] },
        { name: 'donchian', category: 'volatility', description: 'Donchian channel — N-bar high/low.', configurable_params: [{ name: 'length', type: 'int', default: 20, min: 2, max: 200, description: 'Window length.' }] },
      ],
    },
    {
      name: 'volume',
      indicators: [
        { name: 'obv', category: 'volume', description: 'On-balance volume.',  configurable_params: [] },
        { name: 'cmf', category: 'volume', description: 'Chaikin money flow.', configurable_params: [{ name: 'length', type: 'int', default: 20, min: 2, max: 200, description: 'Window length.' }] },
        { name: 'mfi', category: 'volume', description: 'Money flow index.',   configurable_params: [{ name: 'length', type: 'int', default: 14, min: 2, max: 200, description: 'Window length.' }] },
      ],
    },
  ]);

  private readonly _addedKeys = signal<readonly string[]>([]);
  protected readonly activeKeys = computed(() => this._addedKeys());
  protected readonly lastEvent = signal('(none)');

  protected onAdd(e: IndicatorPickerAdd): void {
    this._addedKeys.update(prev => [...prev, e.name]);
    this.lastEvent.set(`add ${e.name} ${JSON.stringify(e.params)}`);
  }

  protected onAddInstance(e: IndicatorPickerAdd): void {
    this._addedKeys.update(prev => [...prev, e.name]);
    this.lastEvent.set(`addInstance ${e.name} ${JSON.stringify(e.params)}`);
  }

  protected onPreview(e: IndicatorPickerPreview): void {
    this.lastEvent.set(`preview ${e.name} active=${e.active}`);
  }
}
