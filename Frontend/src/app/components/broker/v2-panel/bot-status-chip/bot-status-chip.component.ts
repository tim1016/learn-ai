import { ChangeDetectionStrategy, Component, input } from '@angular/core';

/** Compact text-plus-icon status; color is supporting information only. */
@Component({
  selector: 'app-bot-status-chip',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <span
      class="status-chip"
      [class.status-chip--attention]="needsAttention()"
      [attr.aria-label]="needsAttention() ? statusLabel() + ', needs attention' : statusLabel()"
    >
      <span class="status-chip__mark" aria-hidden="true"></span>
      {{ statusLabel() }}
    </span>
  `,
  styles: `
    .status-chip {
      display: inline-flex;
      align-items: center;
      gap: var(--space-1);
      color: var(--text-primary);
      font-size: var(--fs-xs);
      font-weight: var(--fw-semi);
    }
    .status-chip__mark {
      width: 0.5rem;
      height: 0.5rem;
      border-radius: 50%;
      background: var(--bull);
    }
    .status-chip--attention .status-chip__mark { background: var(--warn); }
  `,
})
export class BotStatusChipComponent {
  readonly statusLabel = input.required<string>();
  readonly needsAttention = input(false);
}
