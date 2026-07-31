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
  private readonly fixtureById = new Map<string, AlpacaBotControlFixture>(
    this.fixtures.map((fixture) => [fixture.scenario_id, fixture]),
  );
  protected readonly selectedFixture = signal<AlpacaBotControlFixture>(
    this.fixtures[0],
  );
  protected readonly selectedScenarioId = computed(
    () => this.selectedFixture().scenario_id,
  );
  protected readonly activeLens = signal<'trader' | 'operator'>('trader');

  protected selectScenario(scenarioId: string): void {
    const fixture = this.fixtureById.get(scenarioId);
    if (fixture === undefined) {
      throw new Error(`Unknown Alpaca diagnostic scenario: ${scenarioId}`);
    }
    this.selectedFixture.set(fixture);
  }

  protected selectLens(lens: 'trader' | 'operator'): void {
    this.activeLens.set(lens);
  }
}
