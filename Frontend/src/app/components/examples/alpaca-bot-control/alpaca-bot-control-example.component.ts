import {
  ChangeDetectionStrategy,
  Component,
  computed,
  signal,
} from '@angular/core';

import {
  STATIC_ALPACA_BOT_CONTROL_FIXTURES,
  type AlpacaBotControlFixture,
} from './alpaca-bot-control-fixtures';
import { AlpacaBotControlOperatorDiagnosticComponent } from './alpaca-bot-control-operator-diagnostic.component';
import { AlpacaBotControlTraderDiagnosticComponent } from './alpaca-bot-control-trader-diagnostic.component';

@Component({
  selector: 'app-alpaca-bot-control-example',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AlpacaBotControlTraderDiagnosticComponent,
    AlpacaBotControlOperatorDiagnosticComponent,
  ],
  templateUrl: './alpaca-bot-control-example.component.html',
  styleUrl: './alpaca-bot-control-example.component.scss',
})
export class AlpacaBotControlExampleComponent {
  protected readonly fixtures = STATIC_ALPACA_BOT_CONTROL_FIXTURES;
  protected readonly selectedScenarioId = signal(this.fixtures[0].scenario_id);
  protected readonly activeLens = signal<'trader' | 'operator'>('trader');

  protected readonly selectedFixture = computed<AlpacaBotControlFixture>(() => {
    const selected = this.fixtures.find(
      (fixture) => fixture.scenario_id === this.selectedScenarioId(),
    );
    return selected ?? this.fixtures[0];
  });

  protected selectScenario(event: Event): void {
    if (event.target instanceof HTMLSelectElement) {
      this.selectedScenarioId.set(event.target.value);
    }
  }

  protected selectLens(lens: 'trader' | 'operator'): void {
    this.activeLens.set(lens);
  }
}
