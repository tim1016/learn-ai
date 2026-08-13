import { fireEvent, render, screen } from '@testing-library/angular';
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
    fireEvent.click(trigger);
    expect(trigger.getAttribute('aria-expanded')).toBe('true');
    expect(document.getElementById(trigger.getAttribute('aria-controls') ?? '')).not.toBeNull();
    expect(screen.getByRole('link', { name: 'Manual order' })).toBeTruthy();
  });
});
