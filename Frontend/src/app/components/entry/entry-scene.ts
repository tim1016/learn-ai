/**
 * Geometry of the entry painting: which part of it a frame shows, how far the
 * pointer shifts it, and where the carved "I'M IN" panel lands on screen.
 *
 * Painting coordinates run 0..1 across and 0..1 down. Depths are read off
 * `assets/entry/depth.png` (1 = nearest). The tablet is one flat depth there,
 * so the carving moves rigidly and the click target can follow it exactly.
 */

/** Width over height of `assets/entry/painting.webp` (2000 x 1091). */
export const PAINTING_ASPECT = 2000 / 1091;
/** Pixel width of the painting; drawing it wider than this only costs GPU time. */
export const PAINTING_WIDTH_PX = 2000;

/** The devil's depth. This depth stays put; nearer things slide one way, farther things the other. */
export const FOCUS_DEPTH = 0.507;
const TABLET_DEPTH = 0.54;

/** The carved panel in the painting: centre, width as a share of the painting's width, shape and slant. */
export const CARVED_PANEL = {
  x: 0.4653,
  y: 0.3472,
  width: 0.0513,
  heightRatio: 0.4264,
  tiltDeg: -10.83,
} as const;

/** Largest shift, in painting widths, for a pointer at the edge of the frame. */
const PARALLAX_STRENGTH = 0.028;
/** Vertical movement reads stronger than horizontal on a wide picture, so it is damped. */
const VERTICAL_DAMPING = 0.55;

export interface FrameSize {
  readonly width: number;
  readonly height: number;
}

/** Where the view is centred on the painting and how far it is pushed in. */
export interface Camera {
  readonly zoom: number;
  readonly cx: number;
  readonly cy: number;
}

/** The part of the painting a frame shows: its centre and how much of the painting it spans. */
export interface SceneView {
  readonly cx: number;
  readonly cy: number;
  readonly spanX: number;
  readonly spanY: number;
}

/** A parallax shift in painting units. */
export interface SceneOffset {
  readonly x: number;
  readonly y: number;
}

/** A point in the frame, each axis running -1..1 from one edge to the other. */
export interface PointerPosition {
  readonly x: number;
  readonly y: number;
}

/** The carved panel on screen, in CSS pixels: its centre and width. */
export interface PanelPlacement {
  readonly x: number;
  readonly y: number;
  readonly width: number;
}

/** The whole painting, centred. */
export const HOME_CAMERA: Camera = { zoom: 1, cx: 0.5, cy: 0.5 };

/**
 * What a frame shows. At zoom 1 the whole painting is in view, whatever the
 * frame's shape: the painting fits the tighter axis and the other axis spans
 * past it, which the page fills with a blurred copy.
 */
export function sceneView(frame: FrameSize, camera: Camera): SceneView {
  const frameAspect = frame.width / frame.height;
  const fitsWidth = frameAspect <= PAINTING_ASPECT;
  const spanX = (fitsWidth ? 1 : frameAspect / PAINTING_ASPECT) / camera.zoom;
  const spanY = (fitsWidth ? PAINTING_ASPECT / frameAspect : 1) / camera.zoom;
  return { spanX, spanY, cx: centreWithin(camera.cx, spanX), cy: centreWithin(camera.cy, spanY) };
}

/** A span of the whole painting or more is centred; a pushed-in span stays inside it. */
function centreWithin(wanted: number, span: number): number {
  if (span >= 1) return 0.5;
  return Math.min(1 - span / 2, Math.max(span / 2, wanted));
}

/** How far the scene shifts for a pointer position. Pushing in softens it. */
export function parallaxOffset(pointer: PointerPosition, zoom: number): SceneOffset {
  const strength = PARALLAX_STRENGTH / Math.sqrt(zoom);
  return {
    x: strength * pointer.x,
    y: strength * pointer.y * VERTICAL_DAMPING * PAINTING_ASPECT,
  };
}

/** Where the carved panel sits in the frame, following the tablet as the scene shifts. */
export function carvedPanelPlacement(
  frame: FrameSize,
  view: SceneView,
  offset: SceneOffset,
): PanelPlacement {
  const lift = TABLET_DEPTH - FOCUS_DEPTH;
  const x = CARVED_PANEL.x - offset.x * lift;
  const y = CARVED_PANEL.y - offset.y * lift;
  return {
    x: ((x - view.cx) / view.spanX + 0.5) * frame.width,
    y: ((y - view.cy) / view.spanY + 0.5) * frame.height,
    width: (CARVED_PANEL.width / view.spanX) * frame.width,
  };
}

/** Eases a 0..1 progress in and out, for the push-in when the page is entered. */
export function easeInOut(progress: number): number {
  return progress < 0.5
    ? 4 * progress * progress * progress
    : 1 - Math.pow(-2 * progress + 2, 3) / 2;
}

/** The camera part-way between two cameras. */
export function cameraBetween(from: Camera, to: Camera, progress: number): Camera {
  const mix = (a: number, b: number) => a + (b - a) * progress;
  return { zoom: mix(from.zoom, to.zoom), cx: mix(from.cx, to.cx), cy: mix(from.cy, to.cy) };
}
