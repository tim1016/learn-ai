import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
} from '@angular/core';
import { TooltipModule } from 'primeng/tooltip';
import { MethodologyDrawerService } from '../methodology-drawer/methodology-drawer.service';
import {
  DOC_REFS,
  type DocRef,
  type DocRefKey,
} from '../../components/research-lab/indicator-reliability/doc-refs';

/**
 * Small ⓘ marker for any metric on the Indicator Reliability page.
 *
 * - On hover, shows a PrimeNG tooltip with the metric's title + definition.
 * - On click, opens the methodology drawer deep-linked to the right section.
 *
 * Usage:
 *   <app-info-icon refKey="confidenceScore" />
 *   <app-info-icon [ref]="{ title, definition, section }" />
 */
@Component({
  selector: 'app-info-icon',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TooltipModule],
  template: `
    <button
      type="button"
      class="info-icon"
      [pTooltip]="tooltipText()"
      tooltipPosition="top"
      [attr.aria-label]="ariaLabel()"
      (click)="onClick($event)"
    >
      <i class="pi pi-info-circle" aria-hidden="true"></i>
    </button>
  `,
  styles: [`
    .info-icon {
      background: transparent;
      border: 0;
      padding: 0 2px;
      color: var(--text-muted);
      cursor: help;
      line-height: 1;
      display: inline-flex;
      align-items: center;
      vertical-align: baseline;
      transition: color 0.1s;
      font-size: 0.72rem;

      i { font-size: 0.78rem; }

      &:hover {
        color: var(--accent);
      }
      &:focus-visible {
        outline: 2px solid var(--accent);
        outline-offset: 2px;
        border-radius: 2px;
      }
    }
  `],
})
export class InfoIconComponent {
  /** Registry key — the typical path. */
  refKey = input<DocRefKey | null>(null);

  /** Escape hatch: pass a DocRef inline (useful for one-offs). */
  ref = input<DocRef | null>(null);

  private svc = inject(MethodologyDrawerService);

  private resolved = computed<DocRef | null>(() => {
    const r = this.ref();
    if (r) return r;
    const key = this.refKey();
    return key ? DOC_REFS[key] : null;
  });

  protected tooltipText = computed(() => {
    const r = this.resolved();
    if (!r) return '';
    return `${r.title} — ${r.definition}  (click for full reference)`;
  });

  protected ariaLabel = computed(() => {
    const r = this.resolved();
    return r ? `Learn more about ${r.title}` : 'More info';
  });

  onClick(ev: MouseEvent): void {
    ev.stopPropagation();
    ev.preventDefault();
    const r = this.resolved();
    if (!r) return;
    this.svc.open(r.section);
  }
}
