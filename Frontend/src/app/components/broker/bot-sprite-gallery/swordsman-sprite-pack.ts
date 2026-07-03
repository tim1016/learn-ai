export type SwordsmanLevel = 1 | 2 | 3;

export interface SwordsmanSpriteState {
  readonly id: string;
  readonly label: string;
  readonly frames: number;
  readonly durationMs: number;
}

export interface SwordsmanFacing {
  readonly id: string;
  readonly label: string;
}

export interface SwordsmanSpriteCard extends SwordsmanSpriteState {
  readonly level: SwordsmanLevel;
  readonly facingId: string;
  readonly primaryColor: string;
  readonly accentColor: string;
  readonly trimColor: string;
}

export const SWORDSMAN_LEVELS: readonly SwordsmanLevel[] = [1, 2, 3];

export const SWORDSMAN_FACINGS: readonly SwordsmanFacing[] = [
  { id: 'east', label: 'East' },
  { id: 'west', label: 'West' },
  { id: 'south', label: 'South' },
  { id: 'north', label: 'North' },
];

export const SWORDSMAN_SPRITE_STATES: readonly SwordsmanSpriteState[] = [
  { id: 'idle', label: 'Idle', frames: 12, durationMs: 960 },
  { id: 'walk', label: 'Walk', frames: 6, durationMs: 540 },
  { id: 'run', label: 'Run', frames: 8, durationMs: 520 },
  { id: 'attack', label: 'Attack', frames: 8, durationMs: 560 },
  { id: 'walk-attack', label: 'Walk Attack', frames: 6, durationMs: 540 },
  { id: 'run-attack', label: 'Run Attack', frames: 8, durationMs: 560 },
  { id: 'hurt', label: 'Hurt', frames: 5, durationMs: 450 },
  { id: 'death', label: 'Death', frames: 7, durationMs: 700 },
];

const LEVEL_PALETTES: Record<
  SwordsmanLevel,
  {
    readonly primaryColor: string;
    readonly accentColor: string;
    readonly trimColor: string;
  }
> = {
  1: { primaryColor: '#4f8fdf', accentColor: '#86d7ff', trimColor: '#24385f' },
  2: { primaryColor: '#6fbc6d', accentColor: '#f0d55f', trimColor: '#2f4d38' },
  3: { primaryColor: '#b46be2', accentColor: '#ff8db3', trimColor: '#4f2f64' },
};

export function buildSwordsmanSpriteCards(
  level: SwordsmanLevel,
  facing: SwordsmanFacing,
): SwordsmanSpriteCard[] {
  const palette = LEVEL_PALETTES[level];
  return SWORDSMAN_SPRITE_STATES.map(state => ({
    ...state,
    ...palette,
    level,
    facingId: facing.id,
  }));
}
