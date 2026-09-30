import { provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { fakeCatalogBot } from '../../../testing/bot-panel-fixtures';
import { HomeBotActionComponent, type StopPhase } from './home-bot-action.component';

const ACCOUNT = { broker: 'alpaca', clerkId: 'clrk_spec', accountId: 'PA9' };

async function renderAction(stopPhase: StopPhase | null = null) {
  const requested: string[] = [];
  const view = await render(HomeBotActionComponent, {
    inputs: { bot: fakeCatalogBot({ strategy_instance_id: 'spy-ema-1' }), account: ACCOUNT, stopPhase },
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

  it('says it is stopping, and takes no second press, while the page’s Stop is sent', async () => {
    const { requested } = await renderAction('sending');

    const stop = screen.getByRole('button', { name: 'Stop spy-ema-1' });
    stop.click();

    expect(stop.textContent?.trim()).toBe('Stopping…');
    expect(stop.hasAttribute('disabled')).toBe(true);
    expect(stop.getAttribute('aria-busy')).toBe('true');
    expect(requested).toEqual([]);
  });

  it('says it is checking, and stays enabled to keep the keyboard, while the page reads the Stop (#2634)', async () => {
    await renderAction('reading');

    const stop = screen.getByRole('button', { name: 'Stop spy-ema-1' });

    expect(stop.textContent?.trim()).toBe('Checking…');
    expect(stop.hasAttribute('disabled')).toBe(false);
    expect(stop.getAttribute('aria-busy')).toBe('true');
  });
});
