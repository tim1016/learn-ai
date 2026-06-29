import { ChangeDetectionStrategy, Component, computed, signal } from '@angular/core';

type ShowcaseMode = 'parallax' | 'menu' | 'arena' | 'layers';

interface DesertScene {
  readonly id: number;
  readonly label: string;
  readonly tone: string;
}

interface LayerPlane {
  readonly id: number;
  readonly label: string;
  readonly drift: number;
}

interface SceneAsset extends DesertScene {
  readonly background: string;
  readonly preview: string;
  readonly planes: LayerAsset[];
}

interface LayerAsset extends LayerPlane {
  readonly image: string;
}

interface ShowcaseModeOption {
  readonly id: ShowcaseMode;
  readonly label: string;
}

const ASSET_ROOT = '/assets/backgrounds/desert-oasis';

const SCENES: DesertScene[] = [
  { id: 1, label: 'Cliff Waterfall', tone: 'oasis pass' },
  { id: 2, label: 'Dune Crossing', tone: 'open route' },
  { id: 3, label: 'Palm Basin', tone: 'quiet camp' },
  { id: 4, label: 'Canyon Pool', tone: 'boss approach' },
];

const PLANES: LayerPlane[] = [
  { id: 1, label: 'Sky and far color', drift: 2 },
  { id: 2, label: 'Rock and oasis set', drift: 6 },
  { id: 3, label: 'Midground dunes', drift: 10 },
  { id: 4, label: 'Foreground forms', drift: 14 },
  { id: 5, label: 'Ground line', drift: 18 },
];

const MODES: ShowcaseModeOption[] = [
  { id: 'parallax', label: 'Parallax' },
  { id: 'menu', label: 'Menu' },
  { id: 'arena', label: 'Arena' },
  { id: 'layers', label: 'Layers' },
];

@Component({
  selector: 'app-desert-oasis-showcase',
  templateUrl: './desert-oasis-showcase.component.html',
  styleUrl: './desert-oasis-showcase.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class DesertOasisShowcaseComponent {
  readonly scenes = SCENES;
  readonly modes = MODES;
  readonly selectedSceneId = signal(SCENES[0].id);
  readonly selectedMode = signal<ShowcaseMode>('parallax');

  readonly selectedScene = computed<SceneAsset>(() => this.sceneAsset(this.selectedSceneId()));

  selectScene(sceneId: number): void {
    this.selectedSceneId.set(sceneId);
  }

  selectMode(mode: ShowcaseMode): void {
    this.selectedMode.set(mode);
  }

  private sceneAsset(sceneId: number): SceneAsset {
    const scene = SCENES.find(item => item.id === sceneId) ?? SCENES[0];
    const basePath = `${ASSET_ROOT}/background-${scene.id}`;

    return {
      ...scene,
      background: `${basePath}/background.png`,
      preview: `${basePath}/preview.png`,
      planes: PLANES.map(plane => ({
        ...plane,
        image: `${basePath}/plan-${plane.id}.png`,
      })),
    };
  }
}
