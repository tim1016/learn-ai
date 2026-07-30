import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { TagModule } from 'primeng/tag';

/**
 * Compact chip for bot status. Renders the backend-authored `status_label`
 * prose and an optional attention marker. `status_label` is backend-authored
 * trader copy — it is rendered unpiped. The attention flag is a code-like
 * boolean that drives a visual warning indicator, not piped text.
 */
@Component({
  selector: 'app-bot-status-chip',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TagModule],
  template: `
    <p-tag
      [value]="statusLabel()"
      [severity]="needsAttention() ? 'warn' : 'secondary'"
    />
    @if (needsAttention()) {
      <span class="attention-dot" aria-label="Needs attention" title="Needs attention">!</span>
    }
  `,
  styles: `
    :host { display: inline-flex; align-items: center; gap: 0.25rem; }
    .attention-dot {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 1rem;
      height: 1rem;
      border-radius: 50%;
      background: var(--p-orange-500, #f97316);
      color: #fff;
      font-size: 0.625rem;
      font-weight: 700;
      line-height: 1;
    }
  `,
})
export class BotStatusChipComponent {
  readonly statusLabel = input.required<string>();
  readonly needsAttention = input(false);
}
