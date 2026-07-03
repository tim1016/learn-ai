import { fireEvent, render, screen } from '@testing-library/angular';
import { BotSpriteGalleryComponent } from './bot-sprite-gallery.component';
import {
  SWORDSMAN_FACINGS,
  SWORDSMAN_SPRITE_STATES,
  buildSwordsmanSpriteCards,
} from './swordsman-sprite-pack';

function renderedSprite(label: string): HTMLElement {
  const stage = screen.getByLabelText(`${label} animation`);
  const sprite = stage.querySelector('.sprite');
  if (!(sprite instanceof HTMLElement)) {
    throw new Error(`Missing rendered sprite for ${label}`);
  }
  return sprite;
}

describe('BotSpriteGalleryComponent', () => {
  it('keeps the swordsman pack metadata explicit', () => {
    const cards = buildSwordsmanSpriteCards(3, SWORDSMAN_FACINGS[3]);
    const runAttack = cards.find(card => card.id === 'run-attack');

    expect(SWORDSMAN_SPRITE_STATES).toHaveLength(8);
    expect(runAttack).toEqual({
      id: 'run-attack',
      label: 'Run Attack',
      frames: 8,
      durationMs: 560,
      level: 3,
      facingId: 'north',
      primaryColor: '#b46be2',
      accentColor: '#ff8db3',
      trimColor: '#4f2f64',
    });
  });

  it('renders the swordsman states as animated sprite cards', async () => {
    const { fixture } = await render(BotSpriteGalleryComponent);

    expect(screen.getByRole('heading', { name: /swordsman sprite states/i })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Idle' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Run Attack' })).toBeTruthy();
    expect(fixture.nativeElement.querySelectorAll('.sprite-stage')).toHaveLength(8);
    expect(renderedSprite('Idle').dataset['state']).toBe('idle');
    expect(renderedSprite('Idle').dataset['level']).toBe('1');
    expect(renderedSprite('Idle').dataset['facing']).toBe('south');
    expect(renderedSprite('Idle').style.getPropertyValue('--sprite-primary')).toBe('#4f8fdf');
    expect(renderedSprite('Idle').style.getPropertyValue('--frame-count')).toBe('12');
  });

  it('switches level and facing controls', async () => {
    const { fixture } = await render(BotSpriteGalleryComponent);

    const levelOne = screen.getByRole('button', { name: 'L1' });
    const levelThree = screen.getByRole('button', { name: 'L3' });
    const south = screen.getByRole('button', { name: 'South' });
    const north = screen.getByRole('button', { name: 'North' });

    fireEvent.click(levelThree);
    fireEvent.click(north);
    fixture.detectChanges();

    expect(levelOne.getAttribute('aria-pressed')).toBe('false');
    expect(levelThree.getAttribute('aria-pressed')).toBe('true');
    expect(south.getAttribute('aria-pressed')).toBe('false');
    expect(north.getAttribute('aria-pressed')).toBe('true');
    expect(renderedSprite('Attack').dataset['level']).toBe('3');
    expect(renderedSprite('Attack').dataset['facing']).toBe('north');
    expect(renderedSprite('Attack').style.getPropertyValue('--sprite-accent')).toBe('#ff8db3');
    expect(renderedSprite('Attack').style.getPropertyValue('--duration')).toBe('560ms');
  });
});
