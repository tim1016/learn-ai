import { ChangeDetectionStrategy, Component, signal } from '@angular/core';
import { disabled, form, FormField } from '@angular/forms/signals';
import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { REENTRY_CHOICES } from '../choices';
import type { ReentryKind } from '../options-backtest.types';
import { PillChoiceComponent } from './pill-choice.component';
import { SwitchComponent } from './switch.component';

@Component({
  selector: 'app-controls-host',
  imports: [FormField, PillChoiceComponent, SwitchComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <app-options-backtest-switch [formField]="fields.on" label="Re-entry" />
    <app-options-backtest-pill-choice [formField]="fields.kind" [options]="choices" label="Re-entry kind" [unavailable]="unavailable()" />
    <output data-testid="model">{{ model().on }}|{{ model().kind }}</output>
  `,
})
class HostComponent {
  protected readonly choices = REENTRY_CHOICES;
  protected readonly unavailable = signal<readonly ReentryKind[]>([]);
  protected readonly model = signal<{ on: boolean; kind: ReentryKind }>({ on: true, kind: 'at_once' });
  protected readonly fields = form(this.model, (path) => {
    disabled(path.kind, { when: ({ valueOf }) => !valueOf(path.on) });
  });

  setUnavailable(kinds: readonly ReentryKind[]): void {
    this.unavailable.set(kinds);
  }
}

describe('pill choice and switch', () => {
  it('choosing a pill writes the field it is bound to', async () => {
    await render(HostComponent);

    fireEvent.click(screen.getByRole('radio', { name: 'On momentum' }));

    expect(screen.getByTestId('model').textContent).toBe('true|on_momentum');
    expect((screen.getByRole('radio', { name: 'On momentum' }) as HTMLInputElement).checked).toBe(true);
    expect((screen.getByRole('radio', { name: 'At once' }) as HTMLInputElement).checked).toBe(false);
  });

  it('names its group for a screen reader', async () => {
    await render(HostComponent);

    expect(screen.getByRole('group', { name: 'Re-entry kind' })).toBeTruthy();
  });

  it('greys every pill while its field is disabled, and frees them when it is not', async () => {
    await render(HostComponent);

    fireEvent.click(screen.getByRole('switch', { name: 'Re-entry' }));
    expect((screen.getByRole('switch', { name: 'Re-entry' }) as HTMLInputElement).checked).toBe(false);
    expect(screen.getByTestId('model').textContent).toBe('false|at_once');
    expect((screen.getByRole('group', { name: 'Re-entry kind' }) as HTMLFieldSetElement).disabled).toBe(true);

    fireEvent.click(screen.getByRole('switch', { name: 'Re-entry' }));
    expect((screen.getByRole('group', { name: 'Re-entry kind' }) as HTMLFieldSetElement).disabled).toBe(false);
  });

  it('cannot pick a choice that is unavailable, yet keeps showing the one chosen', async () => {
    const { fixture } = await render(HostComponent);
    fixture.componentInstance.setUnavailable(['on_momentum', 'at_once']);
    fixture.detectChanges();

    expect((screen.getByRole('radio', { name: 'On momentum' }) as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByRole('radio', { name: 'At once' }) as HTMLInputElement).disabled).toBe(false);
    expect((screen.getByRole('radio', { name: 'At once' }) as HTMLInputElement).checked).toBe(true);
  });
});
