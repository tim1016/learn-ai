import { provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { fakeCatalogBot } from '../../../testing/bot-panel-fixtures';
import { HomeBotActionComponent } from './home-bot-action.component';

const ACCOUNT = { broker: 'alpaca', clerkId: 'clrk_spec', accountId: 'PA9' };

async function renderAction(pending = false) {
  const requested: string[] = [];
  const view = await render(HomeBotActionComponent, {
    inputs: { bot: fakeCatalogBot({ strategy_instance_id: 'spy-ema-1' }), account: ACCOUNT, pending },
    on: { stopRequested: (sid: string) => requested.push(sid) },
    providers: [provideRouter([])],
  });
  await view.fixture.whenStable();
  return { view, requested };
}

describe('HomeBotActionComponent', () => {
  it('hands Stop to the page, which asks the owner in the Clerk’s words (#2605)', async () => {
    const { requested } = await renderAction();

    screen.getByRole('button', { name: 'Stop spy-ema-1' }).click();

    expect(requested).toEqual(['spy-ema-1']);
  });

  it('says it is stopping, and takes no second press, while the page’s Stop is in flight', async () => {
    const { requested } = await renderAction(true);

    const stop = screen.getByRole('button', { name: 'Stop spy-ema-1' });
    stop.click();

    expect(stop.textContent).toBe('Stopping…');
    expect(stop.hasAttribute('disabled')).toBe(true);
    expect(requested).toEqual([]);
  });
});
