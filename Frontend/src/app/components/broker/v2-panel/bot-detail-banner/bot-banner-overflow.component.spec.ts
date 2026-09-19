import { fireEvent, render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { BotBannerOverflowComponent } from './bot-banner-overflow.component';

describe('BotBannerOverflowComponent', () => {
  it('reveals projected actions on demand', async () => {
    await render('<app-bot-banner-overflow label="More bot actions"><a href="/manual">Manual order</a></app-bot-banner-overflow>', {
      imports: [BotBannerOverflowComponent],
    });

    expect(screen.queryByRole('link', { name: 'Manual order' })).toBeNull();
    const trigger = screen.getByRole('button', { name: 'More bot actions' });
    expect(trigger.getAttribute('aria-haspopup')).toBeNull();
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    expect(document.getElementById(trigger.getAttribute('aria-controls') ?? '')).not.toBeNull();
    fireEvent.click(trigger);
    expect(trigger.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByRole('link', { name: 'Manual order' })).toBeTruthy();
  });

  it('closes on Escape from inside the menu and hands focus back to the trigger', async () => {
    const user = userEvent.setup();
    const view = await render('<app-bot-banner-overflow label="More bot actions"><a href="/manual">Manual order</a></app-bot-banner-overflow>', {
      imports: [BotBannerOverflowComponent],
    });
    const trigger = screen.getByRole('button', { name: 'More bot actions' });
    await user.click(trigger);
    await view.fixture.whenStable();
    const item = screen.getByRole('link', { name: 'Manual order' });
    item.focus();

    await user.keyboard('{Escape}');
    await view.fixture.whenStable();

    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByRole('link', { name: 'Manual order' })).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it('leaves Escape alone while the menu is closed', async () => {
    const user = userEvent.setup();
    const view = await render('<app-bot-banner-overflow label="More bot actions"><a href="/manual">Manual order</a></app-bot-banner-overflow>', {
      imports: [BotBannerOverflowComponent],
    });
    const trigger = screen.getByRole('button', { name: 'More bot actions' });
    trigger.focus();
    // Whatever sits around the banner must still receive an untouched Escape.
    const escapesReachingDocument: KeyboardEvent[] = [];
    const record = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') escapesReachingDocument.push(event);
    };
    document.addEventListener('keydown', record);

    try {
      await user.keyboard('{Escape}');
      await view.fixture.whenStable();
    } finally {
      document.removeEventListener('keydown', record);
    }

    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    expect(escapesReachingDocument.map((event) => event.defaultPrevented)).toEqual([false]);
  });
});
