import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { fakePickerWorld, flushGate } from '../../../shared/symbol-picker/testing/fake-picker-world';

import { DeployParametersSectionComponent } from './deploy-parameters-section.component';
import type { DeployStrategyParamsSchema } from '../v2-panel/lib/broker-v2-panel.service';

const SCHEMA: DeployStrategyParamsSchema = {
  title: 'SmaCrossoverParams',
  type: 'object',
  properties: {
    short_window: { type: 'integer', default: 10, minimum: 2, maximum: 500, title: 'Short window' },
    gap: { type: 'number', default: 0.2, minimum: 0, title: 'Crossover gap' },
  },
  required: [],
};

describe('DeployParametersSectionComponent', () => {
  it('rejects a partially-numeric string instead of silently truncating it', async () => {
    const parameterChange = vi.fn();
    await render(DeployParametersSectionComponent, {
      inputs: { paramsSchema: SCHEMA, values: {} },
      on: { parameterChange, invalidFieldsChange: vi.fn() },
    });

    const input = screen.getByRole('textbox', { name: 'Short window' });
    fireEvent.input(input, { target: { value: '12abc' } });
    fireEvent.change(input, { target: { value: '12abc' } });

    expect(parameterChange).not.toHaveBeenCalled();
    expect(input.getAttribute('aria-invalid')).toBe('true');
    expect(screen.getByRole('alert')).toBeTruthy();
  });

  it('rejects a blank value rather than silently keeping the previous one', async () => {
    const parameterChange = vi.fn();
    await render(DeployParametersSectionComponent, {
      inputs: { paramsSchema: SCHEMA, values: { short_window: 25 } },
      on: { parameterChange, invalidFieldsChange: vi.fn() },
    });

    const input = screen.getByRole('textbox', { name: 'Short window' });
    fireEvent.change(input, { target: { value: '   ' } });

    expect(parameterChange).not.toHaveBeenCalled();
    expect(input.getAttribute('aria-invalid')).toBe('true');
  });

  it('rejects a non-integer value for an integer field', async () => {
    const parameterChange = vi.fn();
    await render(DeployParametersSectionComponent, {
      inputs: { paramsSchema: SCHEMA, values: {} },
      on: { parameterChange, invalidFieldsChange: vi.fn() },
    });

    const input = screen.getByRole('textbox', { name: 'Short window' });
    fireEvent.change(input, { target: { value: '10.5' } });

    expect(parameterChange).not.toHaveBeenCalled();
    expect(input.getAttribute('aria-invalid')).toBe('true');
  });

  it('accepts a strictly-valid number and clears the invalid state', async () => {
    const parameterChange = vi.fn();
    const invalidFieldsChange = vi.fn();
    await render(DeployParametersSectionComponent, {
      inputs: { paramsSchema: SCHEMA, values: {} },
      on: { parameterChange, invalidFieldsChange },
    });

    const input = screen.getByRole('textbox', { name: 'Short window' });
    fireEvent.change(input, { target: { value: '25' } });

    expect(parameterChange).toHaveBeenCalledWith({ field: 'short_window', value: 25 });
    expect(input.getAttribute('aria-invalid')).toBeNull();
    expect(invalidFieldsChange).toHaveBeenLastCalledWith(new Set());
  });

  it('shows the settings in use without an editor when the world trades only qualified settings', async () => {
    await render(DeployParametersSectionComponent, {
      inputs: { paramsSchema: SCHEMA, values: { short_window: 25 }, editable: false },
      on: { parameterChange: vi.fn(), invalidFieldsChange: vi.fn() },
    });

    expect(screen.queryByRole('textbox', { name: 'Short window' })).toBeNull();
    const inUse = screen.getByLabelText('Settings in use');
    expect(inUse.textContent).toContain('Short window');
    expect(inUse.textContent).toContain('25');
  });

  it('never reaches the host with an empty invalid set once fixed', async () => {
    const invalidFieldsChange = vi.fn();
    await render(DeployParametersSectionComponent, {
      inputs: { paramsSchema: SCHEMA, values: {} },
      on: { parameterChange: vi.fn(), invalidFieldsChange },
    });

    const input = screen.getByRole('textbox', { name: 'Short window' });
    fireEvent.change(input, { target: { value: 'abc' } });
    expect(invalidFieldsChange).toHaveBeenLastCalledWith(new Set(['short_window']));

    fireEvent.change(input, { target: { value: '25' } });
    expect(invalidFieldsChange).toHaveBeenLastCalledWith(new Set());
  });
});

describe('qualified configuration action', () => {
  const qualified = {
    symbol: 'AAPL', parameters: { short_window: 10, gap: 0.2 },
    explanation: 'This exact tuple is covered. Independent deployment checks still apply.',
  };

  it('leaves edits alone until the operator explicitly chooses the offered exact tuple', async () => {
    const world = fakePickerWorld();
    const selected = vi.fn();
    const dryRun = vi.fn();
    const parameters = vi.fn();
    await render(DeployParametersSectionComponent, {
      inputs: { paramsSchema: SCHEMA, values: { short_window: 25 }, qualifiedConfiguration: qualified },
      providers: world.providers,
      on: { qualifiedConfigurationSelected: selected, parameterChange: parameters, dryRunRequested: dryRun },
    });
    expect(selected).not.toHaveBeenCalled();
    expect(parameters).not.toHaveBeenCalled();
    expect((screen.getByRole('textbox', { name: 'Short window' }) as HTMLInputElement).value).toBe('25');
    fireEvent.click(screen.getByRole('button', { name: 'Try other settings in Dry Run' }));
    expect(dryRun).toHaveBeenCalledOnce();
    expect(selected).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Use qualified configuration' }));
    expect(selected).toHaveBeenCalledWith(qualified);
  });

  it('applies the exact preset only after the shared instrument card confirms lake coverage', async () => {
    const world = fakePickerWorld([], [{ symbol: 'AAPL', name: 'Apple', exchange: 'NASDAQ', asset_class: 'us_equity', status: 'active' }]);
    const selected = vi.fn();
    const { fixture } = await render(DeployParametersSectionComponent, {
      inputs: { paramsSchema: SCHEMA, values: {}, qualifiedConfiguration: qualified },
      providers: world.providers,
      on: { qualifiedConfigurationSelected: selected },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Use qualified configuration' }));
    expect(world.coverage.ensureCalls).toEqual([{ symbol: 'AAPL', mode: 'polygon_split_adjusted' }]);
    expect(selected).not.toHaveBeenCalled();
    await flushGate(fixture);
    expect(selected).toHaveBeenCalledWith(qualified);
  });

  it('cannot invent membership for a preset symbol missing from the joined universe', async () => {
    const selected = vi.fn();
    await render(DeployParametersSectionComponent, {
      inputs: { paramsSchema: SCHEMA, values: {}, qualifiedConfiguration: { ...qualified, symbol: 'NOT-LISTED' } },
      providers: fakePickerWorld().providers,
      on: { qualifiedConfigurationSelected: selected },
    });
    expect((screen.getByRole('button', { name: 'Use qualified configuration' }) as HTMLButtonElement).disabled).toBe(true);
    expect(selected).not.toHaveBeenCalled();
  });
});
