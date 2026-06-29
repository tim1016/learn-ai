import { render, screen } from '@testing-library/angular';
import { DesertOasisShowcaseComponent } from './desert-oasis-showcase.component';

describe('DesertOasisShowcaseComponent', () => {
  it('renders the scene lab with the default parallax showcase', async () => {
    const { fixture } = await render(DesertOasisShowcaseComponent);

    expect(screen.getByRole('heading', { name: /desert oasis scene lab/i })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Cliff Waterfall' })).toBeTruthy();
    expect(fixture.nativeElement.querySelectorAll('.parallax-plane')).toHaveLength(5);
    expect(screen.getAllByRole('button', { name: /parallax|menu|arena|layers/i })).toBeTruthy();
  });

  it('switches scene and showcase mode', async () => {
    const { fixture } = await render(DesertOasisShowcaseComponent);

    screen.getByRole('button', { name: '4' }).click();
    screen.getByRole('button', { name: 'Arena' }).click();
    fixture.detectChanges();

    expect(screen.getByRole('heading', { name: 'Canyon Pool' })).toBeTruthy();
    expect(fixture.nativeElement.querySelector('.arena-frame')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('.hero-marker')).toBeTruthy();
  });
});
