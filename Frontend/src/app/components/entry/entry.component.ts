import { NgOptimizedImage } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  afterNextRender,
  inject,
  signal,
  viewChild,
} from '@angular/core';
import { Router } from '@angular/router';

import { mediaQuerySignal } from '../../shared/media-query';
import {
  CARVED_PANEL,
  HOME_CAMERA,
  PAINTING_WIDTH_PX,
  cameraBetween,
  carvedPanelPlacement,
  easeInOut,
  parallaxOffset,
  sceneView,
  type Camera,
  type FrameSize,
  type PointerPosition,
  type SceneOffset,
} from './entry-scene';
import { createSceneRenderer, type SceneRenderer } from './entry-scene-renderer';

/** Where "I'm In" leads. There is no sign-in: the click only opens the app. */
const DESTINATION = '/data-lab';
const ASSETS = '/assets/entry';

const PUSH_IN_ZOOM = 1.8;
const PUSH_IN_MS = 750;
/** With the pointer still this long, the scene drifts by itself. */
const DRIFT_AFTER_MS = 2500;
/** How lit the carving is with nothing pointing at it; it breathes around this. */
const RESTING_GLOW = 0.22;
/** Time constants for the pointer and the glow settling on their targets. */
const POINTER_SETTLE_MS = 270;
const GLOW_SETTLE_MS = 160;
const NO_SHIFT: SceneOffset = { x: 0, y: 0 };

/** The camera pushing in on the carving once the page is entered. */
interface PushIn {
  readonly from: Camera;
  readonly to: Camera;
  readonly startedAt: number;
}

/**
 * The page the app opens on: the painting, shown whole with depth, and the
 * "I'M IN" carved into its tablet as the way in.
 *
 * The lettering is part of the painting, so the real control is an unpainted
 * button kept over the carving as the scene shifts; the glow that answers a
 * pointer or keyboard focus is drawn inside the painting. Where WebGL is
 * missing the plain painting stays up and the button works the same.
 */
@Component({
  selector: 'app-entry',
  templateUrl: './entry.component.html',
  styleUrl: './entry.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgOptimizedImage],
})
export class EntryComponent {
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);
  private readonly reducedMotion = mediaQuerySignal('(prefers-reduced-motion: reduce)');
  private readonly stage = viewChild.required<ElementRef<HTMLElement>>('stage');
  private readonly scene = viewChild.required<ElementRef<HTMLCanvasElement>>('scene');
  private readonly enterButton = viewChild.required<ElementRef<HTMLButtonElement>>('enterButton');

  protected readonly paintingSrc = `${ASSETS}/painting.webp`;
  protected readonly panelRatio = CARVED_PANEL.heightRatio;
  protected readonly panelTilt = `${CARVED_PANEL.tiltDeg}deg`;
  /** The plain painting is what shows: before the depth scene is ready, and for good without WebGL. */
  protected readonly flat = signal(true);
  /** "I'm In" was pressed: the page fades and the button stops answering. */
  protected readonly entering = signal(false);

  // What follows changes every frame and is written straight to the canvas and
  // the button, so it stays out of signals and out of change detection.
  private renderer: SceneRenderer | null = null;
  private frame: FrameSize = { width: 1, height: 1 };
  private camera: Camera = HOME_CAMERA;
  private pushIn: PushIn | null = null;
  private pointerTarget: PointerPosition = { x: 0, y: 0 };
  private pointer: PointerPosition = { x: 0, y: 0 };
  private pointerMovedAt = Number.NEGATIVE_INFINITY;
  private pointed = false;
  private glow = 0;
  private destroyed = false;

  constructor() {
    afterNextRender(() => {
      const stage = this.stage().nativeElement;
      const observer = new ResizeObserver(() => this.measure());
      observer.observe(stage);
      const trackPointer = (event: PointerEvent) => this.trackPointer(event);
      stage.addEventListener('pointermove', trackPointer);

      this.measure();
      let last = performance.now();
      this.paint(last, 0);
      let frameId = 0;
      const next = (now: number): void => {
        // A tab coming back from the background reports one huge gap; cap it.
        this.paint(now, Math.min(now - last, 100));
        last = now;
        frameId = requestAnimationFrame(next);
      };
      frameId = requestAnimationFrame(next);

      this.destroyRef.onDestroy(() => {
        this.destroyed = true;
        cancelAnimationFrame(frameId);
        observer.disconnect();
        stage.removeEventListener('pointermove', trackPointer);
        this.renderer?.dispose();
      });
      void this.startScene();
    });
  }

  protected enter(): void {
    if (this.entering()) return;
    this.entering.set(true);
    if (this.renderer === null || this.reducedMotion()) {
      void this.open();
      return;
    }
    this.pushIn = {
      from: this.camera,
      to: { zoom: this.camera.zoom * PUSH_IN_ZOOM, cx: CARVED_PANEL.x, cy: CARVED_PANEL.y },
      startedAt: performance.now(),
    };
  }

  /** A pointer over the tablet, or keyboard focus on it, lights the carving. */
  protected point(on: boolean): void {
    this.pointed = on;
  }

  private async open(): Promise<void> {
    let opened = false;
    try {
      opened = await this.router.navigateByUrl(DESTINATION);
    } finally {
      // A refused or failed navigation must not leave a black page behind.
      if (!opened) {
        this.pushIn = null;
        this.camera = HOME_CAMERA;
        this.entering.set(false);
      }
    }
  }

  private async startScene(): Promise<void> {
    try {
      const [painting, depth, glow] = await Promise.all(
        ['painting.webp', 'depth.png', 'glow.png'].map((name) => loadPicture(`${ASSETS}/${name}`)),
      );
      if (this.destroyed) return;
      this.renderer = createSceneRenderer(this.scene().nativeElement, { painting, depth, glow });
    } catch (error) {
      // The plain painting is already up and the button works on it.
      console.warn('[Entry] depth scene unavailable; showing the plain painting', error);
      return;
    }
    if (this.renderer === null) return;
    this.measure();
    this.flat.set(false);
  }

  private measure(): void {
    const rect = this.stage().nativeElement.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    this.frame = { width: rect.width, height: rect.height };
    // The painting holds no more detail than its own width, however dense the screen.
    const pixelRatio = Math.min(window.devicePixelRatio || 1, 2, PAINTING_WIDTH_PX / rect.width);
    this.renderer?.resize(this.frame, pixelRatio);
  }

  private trackPointer(event: PointerEvent): void {
    const rect = this.stage().nativeElement.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    this.pointerTarget = {
      x: clampUnit(((event.clientX - rect.left) / rect.width) * 2 - 1),
      y: clampUnit(((event.clientY - rect.top) / rect.height) * 2 - 1),
    };
    this.pointerMovedAt = performance.now();
  }

  private paint(now: number, elapsed: number): void {
    if (this.pushIn !== null) {
      const progress = Math.min(1, (now - this.pushIn.startedAt) / PUSH_IN_MS);
      this.camera = cameraBetween(this.pushIn.from, this.pushIn.to, easeInOut(progress));
      if (progress >= 1) {
        this.pushIn = null;
        void this.open();
      }
    }

    const calm = this.reducedMotion();
    const drifting = !calm && now - this.pointerMovedAt > DRIFT_AFTER_MS;
    const target = drifting
      ? { x: Math.sin(now * 0.00042) * 0.7, y: Math.cos(now * 0.00031) * 0.45 }
      : this.pointerTarget;
    const pointerStep = settle(elapsed, POINTER_SETTLE_MS);
    this.pointer = {
      x: this.pointer.x + (target.x - this.pointer.x) * pointerStep,
      y: this.pointer.y + (target.y - this.pointer.y) * pointerStep,
    };
    const resting = RESTING_GLOW * (calm ? 0.8 : 0.62 + 0.38 * Math.sin(now * 0.0019));
    const wanted = this.pointed || this.entering() ? 1 : resting;
    this.glow += (wanted - this.glow) * settle(elapsed, GLOW_SETTLE_MS);

    const view = sceneView(this.frame, this.camera);
    const offset = this.renderer === null ? NO_SHIFT : parallaxOffset(this.pointer, this.camera.zoom);
    this.renderer?.draw(view, offset, this.glow);

    const panel = carvedPanelPlacement(this.frame, view, offset);
    const style = this.enterButton().nativeElement.style;
    style.setProperty('--entry-panel-x', `${panel.x.toFixed(1)}px`);
    style.setProperty('--entry-panel-y', `${panel.y.toFixed(1)}px`);
    style.setProperty('--entry-panel-width', `${panel.width.toFixed(1)}px`);
  }
}

/** The share of the way to a target covered in `elapsed` ms, the same at any frame rate. */
function settle(elapsed: number, timeConstantMs: number): number {
  return 1 - Math.exp(-elapsed / timeConstantMs);
}

function clampUnit(value: number): number {
  return Math.min(1, Math.max(-1, value));
}

function loadPicture(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const picture = new Image();
    picture.onload = () => resolve(picture);
    picture.onerror = () => reject(new Error(`Entry picture did not load: ${src}`));
    picture.src = src;
  });
}
