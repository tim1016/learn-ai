import { render, screen } from '@testing-library/angular';
import { BotSpriteGalleryComponent } from './bot-sprite-gallery.component';

describe('BotSpriteGalleryComponent', () => {
  it('renders the swordsman states as animated sprite cards', async () => {
    const { fixture } = await render(BotSpriteGalleryComponent);

    expect(screen.getByRole('heading', { name: /swordsman sprite states/i })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Idle' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Run Attack' })).toBeTruthy();
    expect(fixture.nativeElement.querySelectorAll('.sprite-stage')).toHaveLength(8);
  });

  it('switches level and facing controls', async () => {
    const { fixture } = await render(BotSpriteGalleryComponent);

    const levelThree = screen.getByRole('button', { name: 'L3' });
    const north = screen.getByRole('button', { name: 'North' });

    levelThree.click();
    north.click();
    fixture.detectChanges();

    expect(levelThree.getAttribute('aria-pressed')).toBe('true');
    expect(north.getAttribute('aria-pressed')).toBe('true');
  });
});
