import { ChangeDetectionStrategy, Component, computed, signal } from '@angular/core';

type SwordsmanLevel = 1 | 2 | 3;

interface SpriteState {
  readonly id: string;
  readonly label: string;
  readonly fileToken: string;
  readonly frames: number;
  readonly durationMs: number;
}

interface Facing {
  readonly id: string;
  readonly label: string;
  readonly row: number;
}

interface SpriteCard extends SpriteState {
  readonly image: string;
  readonly sheetWidth: number;
  readonly rowOffset: number;
}

const FRAME_SIZE = 64;

const LEVELS: SwordsmanLevel[] = [1, 2, 3];

const FACINGS: Facing[] = [
  { id: 'east', label: 'East', row: 0 },
  { id: 'west', label: 'West', row: 1 },
  { id: 'south', label: 'South', row: 2 },
  { id: 'north', label: 'North', row: 3 },
];

const SPRITE_STATES: SpriteState[] = [
  { id: 'idle', label: 'Idle', fileToken: 'Idle', frames: 12, durationMs: 960 },
  { id: 'walk', label: 'Walk', fileToken: 'Walk', frames: 6, durationMs: 540 },
  { id: 'run', label: 'Run', fileToken: 'Run', frames: 8, durationMs: 520 },
  { id: 'attack', label: 'Attack', fileToken: 'attack', frames: 8, durationMs: 560 },
  { id: 'walk-attack', label: 'Walk Attack', fileToken: 'Walk_Attack', frames: 6, durationMs: 540 },
  { id: 'run-attack', label: 'Run Attack', fileToken: 'Run_Attack', frames: 8, durationMs: 560 },
  { id: 'hurt', label: 'Hurt', fileToken: 'Hurt', frames: 5, durationMs: 450 },
  { id: 'death', label: 'Death', fileToken: 'Death', frames: 7, durationMs: 700 },
];

@Component({
  selector: 'app-bot-sprite-gallery',
  templateUrl: './bot-sprite-gallery.component.html',
  styleUrl: './bot-sprite-gallery.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class BotSpriteGalleryComponent {
  readonly levels = LEVELS;
  readonly facings = FACINGS;

  readonly selectedLevel = signal<SwordsmanLevel>(1);
  readonly selectedFacing = signal<Facing>(FACINGS[2]);

  readonly spriteCards = computed<SpriteCard[]>(() => {
    const level = this.selectedLevel();
    const rowOffset = this.selectedFacing().row * FRAME_SIZE;

    return SPRITE_STATES.map(state => ({
      ...state,
      image: `/assets/sprites/swordsman/lvl${level}/Swordsman_lvl${level}_${state.fileToken}_with_shadow.png`,
      sheetWidth: state.frames * FRAME_SIZE,
      rowOffset,
    }));
  });

  selectLevel(level: SwordsmanLevel): void {
    this.selectedLevel.set(level);
  }

  selectFacing(facing: Facing): void {
    this.selectedFacing.set(facing);
  }
}
