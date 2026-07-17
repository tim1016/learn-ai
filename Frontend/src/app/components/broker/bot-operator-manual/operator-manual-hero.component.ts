import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterLink } from '@angular/router';

interface ControlLoopStage {
  readonly number: string;
  readonly label: string;
  readonly instruction: string;
  readonly icon: string;
}

const CONTROL_LOOP: readonly ControlLoopStage[] = [
  {
    number: '01',
    label: 'Observe',
    instruction: 'Read account truth and bot state.',
    icon: 'pi pi-eye',
  },
  {
    number: '02',
    label: 'Decide',
    instruction: 'Choose the remedy for the right plane.',
    icon: 'pi pi-compass',
  },
  {
    number: '03',
    label: 'Act',
    instruction: 'Use the narrowest safe control.',
    icon: 'pi pi-bolt',
  },
  {
    number: '04',
    label: 'Verify',
    instruction: 'Confirm the new durable truth.',
    icon: 'pi pi-check-circle',
  },
];

@Component({
  selector: 'app-operator-manual-hero',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  templateUrl: './operator-manual-hero.component.html',
  styleUrl: './operator-manual-hero.component.scss',
})
export class OperatorManualHeroComponent {
  readonly controlLoop = CONTROL_LOOP;
}
