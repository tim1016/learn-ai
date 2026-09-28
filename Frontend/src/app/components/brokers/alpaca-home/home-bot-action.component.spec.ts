import { provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { fakeCatalogBot } from '../../../testing/bot-panel-fixtures';
import { HomeBotActionComponent } from './home-bot-action.component';

const ACCOUNT = { broker: 'alpaca', clerkId: 'clrk_spec', accountId: 'PA9' };

async function renderAction() {
  const view = await render(HomeBotActionComponent, {
    inputs: { bot: fakeCatalogBot({ strategy_instance_id: 'spy-ema-1' }), account: ACCOUNT },
    providers: [provideRouter([])],
  });
  await view.fixture.whenStable();
  return view;
}

/**
 * Review B2: focus is moved after the render that draws its target. These
 * specs click and press keys natively and let zoneless change detection run
 * on its own schedule — Testing Library's `fireEvent` runs change detection
 * synchronously, which hid a focus call that ran before the render did.
 */
describe('HomeBotActionComponent', () => {
  it('lands the keyboard on Cancel once the confirmation is drawn', async () => {
    const view = await renderAction();

    screen.getByRole('button', { name: 'Stop spy-ema-1' }).click();
    await view.fixture.whenStable();

    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Cancel' }));
  });

  it('hands the keyboard back to Stop, enabled again, when the confirmation is dismissed', async () => {
    const view = await renderAction();
    screen.getByRole('button', { name: 'Stop spy-ema-1' }).click();
    await view.fixture.whenStable();

    screen.getByRole('button', { name: 'Cancel' }).dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
    );
    await view.fixture.whenStable();

    const stop = screen.getByRole('button', { name: 'Stop spy-ema-1' });
    expect(stop.hasAttribute('disabled')).toBe(false);
    expect(document.activeElement).toBe(stop);
  });
});
