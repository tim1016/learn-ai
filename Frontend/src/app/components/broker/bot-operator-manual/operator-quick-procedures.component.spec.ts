import { provideRouter } from '@angular/router';
import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { OperatorQuickProceduresComponent } from './operator-quick-procedures.component';

describe('OperatorQuickProceduresComponent', () => {
  it('keeps a featured procedure closed after the operator closes it', async () => {
    const { fixture } = await render(OperatorQuickProceduresComponent, {
      providers: [provideRouter([])],
    });

    const featuredProcedure = screen.getByText('Create and start a bot').closest('details');
    if (!featuredProcedure) throw new Error('Expected the featured quick procedure.');
    expect(featuredProcedure.open).toBe(true);

    featuredProcedure.open = false;
    fireEvent(featuredProcedure, new Event('toggle'));
    fixture.detectChanges();

    expect(featuredProcedure.open).toBe(false);
  });
});
