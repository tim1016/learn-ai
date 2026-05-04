import { Directive, ElementRef, Renderer2, computed, effect, inject, input } from '@angular/core';
import { BrokerHealthService } from '../../services/broker-health.service';

const DEFAULT_TOOLTIP = 'Disabled until IBKR is connected to a paper account (DU…)';

/**
 * ``*appPaperOnly`` — generalizes the per-control disable pattern
 * already used on ``/broker/orders`` (every input gates on
 * ``isPaperConnected()``). Apply to any control that mutates broker
 * state and the harness will:
 *
 *   - set the host's ``disabled`` property to ``true`` whenever
 *     the broker is not connected to a paper account, and clear it
 *     when paper-connected status returns;
 *   - mirror the state to ``aria-disabled`` for assistive tech;
 *   - apply a ``title`` tooltip explaining why, so users who hover
 *     a disabled control don't have to leave the page to find out.
 *
 * The directive does not stop click handlers from firing on
 * non-form elements; pair with ``[disabled]`` or a guarded handler
 * for those. Forms benefit immediately because Angular respects
 * the ``disabled`` attribute on inputs / buttons.
 */
@Directive({
  selector: '[appPaperOnly]',
})
export class PaperOnlyDirective {
  private readonly health = inject(BrokerHealthService);
  private readonly host = inject(ElementRef<HTMLElement>);
  private readonly renderer = inject(Renderer2);

  /** Optional tooltip override for unusual hosts (e.g. links). */
  readonly tooltip = input<string | undefined>(undefined, { alias: 'appPaperOnlyTooltip' });

  /** When true, the directive is a no-op — useful for tests. */
  readonly bypass = input(false, { alias: 'appPaperOnlyBypass' });

  private readonly disabled = computed(() => !this.bypass() && !this.health.isPaperConnected());

  constructor() {
    effect(() => {
      const isDisabled = this.disabled();
      const el = this.host.nativeElement;
      const tooltipText = this.tooltip() ?? DEFAULT_TOOLTIP;

      if ('disabled' in el) {
        (el as HTMLElement & { disabled?: boolean }).disabled = isDisabled;
      }
      this.renderer.setAttribute(el, 'aria-disabled', String(isDisabled));
      if (isDisabled) {
        this.renderer.setAttribute(el, 'title', tooltipText);
      } else {
        this.renderer.removeAttribute(el, 'title');
      }
    });
  }
}
