import { describe, expect, it } from 'vitest';

import {
  CARVED_PANEL,
  HOME_CAMERA,
  PAINTING_ASPECT,
  carvedPanelPlacement,
  parallaxOffset,
  sceneView,
} from './entry-scene';

const NO_SHIFT = { x: 0, y: 0 };

describe('entry scene', () => {
  it.each([
    ['a laptop, taller than the painting', { width: 1440, height: 860 }],
    ['an ultrawide monitor, wider than the painting', { width: 2560, height: 1080 }],
    ['a phone held upright', { width: 400, height: 800 }],
  ])('shows the whole painting on %s', (_name, frame) => {
    const view = sceneView(frame, HOME_CAMERA);

    // A span of 1 is the full painting on that axis; nothing is cropped.
    expect(view.spanX).toBeGreaterThanOrEqual(1);
    expect(view.spanY).toBeGreaterThanOrEqual(1);
    expect(Math.min(view.spanX, view.spanY)).toBe(1);
    expect([view.cx, view.cy]).toEqual([0.5, 0.5]);
  });

  it('puts the click target on the carved panel', () => {
    const frame = { width: 1440, height: 860 };

    const panel = carvedPanelPlacement(frame, sceneView(frame, HOME_CAMERA), NO_SHIFT);

    // The painting fills the 1440 px width, so it is 1440 / aspect tall and
    // centred, leaving equal bars above and below.
    const paintingHeight = 1440 / PAINTING_ASPECT;
    const bar = (860 - paintingHeight) / 2;
    expect(panel.x).toBeCloseTo(CARVED_PANEL.x * 1440, 6);
    expect(panel.y).toBeCloseTo(bar + CARVED_PANEL.y * paintingHeight, 6);
    expect(panel.width).toBeCloseTo(CARVED_PANEL.width * 1440, 6);
  });

  it('keeps the click target on the carving as the scene shifts', () => {
    const frame = { width: 1440, height: 860 };
    const view = sceneView(frame, HOME_CAMERA);
    const still = carvedPanelPlacement(frame, view, NO_SHIFT);

    const shifted = carvedPanelPlacement(frame, view, parallaxOffset({ x: 1, y: 1 }, 1));

    // The tablet is nearer than the depth that stays put, so it slides against
    // the pointer — by a pixel or two, never off the carving.
    expect(shifted.x).toBeLessThan(still.x);
    expect(shifted.y).toBeLessThan(still.y);
    expect(still.x - shifted.x).toBeLessThan(still.width / 10);
    expect(still.y - shifted.y).toBeLessThan(still.width / 10);
  });

  it('keeps a pushed-in view inside the painting', () => {
    const frame = { width: 1440, height: 860 };

    const view = sceneView(frame, { zoom: 4, cx: 0, cy: 1 });

    expect(view.cx - view.spanX / 2).toBeCloseTo(0, 9);
    expect(view.cy + view.spanY / 2).toBeCloseTo(1, 9);
  });
});
