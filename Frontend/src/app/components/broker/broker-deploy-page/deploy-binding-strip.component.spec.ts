import { provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { DeployBotStrategy } from '../v2-panel/lib/broker-v2-panel.service';
import { DeployBindingStripComponent } from './deploy-binding-strip.component';

function strategy(overrides: Partial<DeployBotStrategy>): DeployBotStrategy {
  return {
    strategy_key: 'ema_crossover_signal',
    label: 'EMA Crossover Signal',
    explanation: 'Validated EMA crossover strategy.',
    validation_case_symbol: 'SPY',
    validation_case_parameters: {},
    golden_validation_scope: false,
    evidence_status: 'accepted',
    paper_access_state: 'not_required',
    selectable: true,
    admissible_modes: ['paper'],
    override_explanation: null,
    blocked_explanation: null,
    params_schema: { properties: {}, required: [] },
    ...overrides,
  } as DeployBotStrategy;
}

async function renderStrip(strategies: DeployBotStrategy[], strategyKey: string) {
  return render(DeployBindingStripComponent, {
    providers: [provideRouter([])],
    inputs: { strategies, strategyKey: strategyKey as DeployBotStrategy['strategy_key'] },
  });
}

describe('DeployBindingStripComponent evidence chip', () => {
  it('names an accepted strategy’s evidence as accepted', async () => {
    await renderStrip([strategy({})], 'ema_crossover_signal');

    expect(screen.getByText('✓ Accepted evidence')).toBeTruthy();
  });

  it.each([
    ['evidence_only', 'Evidence only'],
    ['blocked', 'Blocked'],
  ] as const)('names a %s strategy as “%s”, never as accepted', async (status, chip) => {
    await renderStrip([strategy({ evidence_status: status })], 'ema_crossover_signal');

    expect(screen.getByText(chip)).toBeTruthy();
    expect(screen.queryByText('✓ Accepted evidence')).toBeNull();
  });

  it('claims no evidence before a strategy is chosen', async () => {
    await renderStrip([strategy({}), strategy({ strategy_key: 'other', label: 'Other' })], '');

    expect(screen.queryByText('✓ Accepted evidence')).toBeNull();
    expect(screen.queryByText('Evidence only')).toBeNull();
  });
});
