import {
  ChangeDetectionStrategy,
  Component,
  input,
  output,
} from '@angular/core';

/** Shared collapsed card shell for on-demand operator evidence. */
@Component({
  selector: 'app-operator-disclosure-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './operator-disclosure-card.component.html',
  styleUrl: './operator-disclosure-card.component.scss',
})
export class OperatorDisclosureCardComponent {
  readonly title = input.required<string>();
  readonly hint = input.required<string>();
  readonly expandedChange = output<boolean>();

  protected onToggle(event: Event): void {
    this.expandedChange.emit((event.currentTarget as HTMLDetailsElement).open);
  }
}
