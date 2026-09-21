import { ChangeDetectionStrategy, Component, input } from '@angular/core';

@Component({
  selector: 'app-instrument-held-span',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './instrument-held-span.component.html',
  styleUrl: './instrument-held-span.component.scss',
})
export class InstrumentHeldSpanComponent {
  readonly firstHeld = input<string | null>(null);
  readonly lastHeld = input.required<string>();
}
