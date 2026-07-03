import { ChangeDetectionStrategy, Component, computed, signal } from '@angular/core';
import {
  SWORDSMAN_FACINGS,
  SWORDSMAN_LEVELS,
  buildSwordsmanSpriteCards,
} from './swordsman-sprite-pack';
import type { SwordsmanFacing, SwordsmanLevel } from './swordsman-sprite-pack';

@Component({
  selector: 'app-bot-sprite-gallery',
  templateUrl: './bot-sprite-gallery.component.html',
  styleUrl: './bot-sprite-gallery.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class BotSpriteGalleryComponent {
  readonly levels = SWORDSMAN_LEVELS;
  readonly facings = SWORDSMAN_FACINGS;

  readonly selectedLevel = signal<SwordsmanLevel>(1);
  readonly selectedFacing = signal<SwordsmanFacing>(SWORDSMAN_FACINGS[2]);

  readonly spriteCards = computed(() =>
    buildSwordsmanSpriteCards(this.selectedLevel(), this.selectedFacing())
  );

  selectLevel(level: SwordsmanLevel): void {
    this.selectedLevel.set(level);
  }

  selectFacing(facing: SwordsmanFacing): void {
    this.selectedFacing.set(facing);
  }
}
