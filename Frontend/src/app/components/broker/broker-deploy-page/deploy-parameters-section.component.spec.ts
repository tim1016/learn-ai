import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

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
