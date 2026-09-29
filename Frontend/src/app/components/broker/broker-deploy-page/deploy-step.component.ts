import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  Injector,
  afterNextRender,
  computed,
  inject,
  input,
  output,
  viewChild,
} from '@angular/core';

let nextStepId = 0;

/** A step's state in two or three words, beside its heading. */
export interface DeployStepStatus {
  readonly label: string;
  readonly tone: 'ready' | 'todo';
}

/**
 * One numbered step of Deploy (PRD #2560 D8): What → How → Money → Confirm.
 *
 * Where the page has room the four steps stand side by side as columns and
 * stay open; the host passes `foldable` only where they stack. A foldable
 * step shows its body while open and a one-line summary with Edit once
 * folded, and offers Done once its status is Ready — the chip beside the
 * heading says what it still needs. The body stays in the page while folded
 * (only hidden), so the pickers and editors inside keep their state. Focus
 * follows the owner: Edit lands in the opened body, Done returns to the
 * step's Edit button.
 */
@Component({
  selector: 'app-deploy-step',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './deploy-step.component.html',
  styleUrl: './deploy-step.component.scss',
})
export class DeployStepComponent {
  readonly number = input.required<number>();
  readonly heading = input.required<string>();
  /** The folded step's one line. */
  readonly summary = input('');
  /** Whether a foldable step is open; a step that cannot fold always is. */
  readonly open = input(true);
  readonly status = input<DeployStepStatus | null>(null);
  /** Offers Edit / Done — only where the steps stack. */
  readonly foldable = input(false);

  readonly edit = output();
  readonly done = output();

  private readonly injector = inject(Injector);
  private readonly id = nextStepId++;
  protected readonly headingId = `deploy-step-${this.id}-title`;
  protected readonly bodyId = `deploy-step-${this.id}-body`;
  protected readonly statusId = `deploy-step-${this.id}-status`;

  protected readonly expanded = computed(() => !this.foldable() || this.open());
  /** Done waits for the step to be Ready. */
  protected readonly canFold = computed(() => this.status()?.tone === 'ready');

  private readonly toggle = viewChild<ElementRef<HTMLButtonElement>>('toggle');
  private readonly body = viewChild.required<ElementRef<HTMLElement>>('body');

  protected expand(): void {
    this.edit.emit();
    afterNextRender({ write: () => this.body().nativeElement.focus() }, { injector: this.injector });
  }

  protected fold(): void {
    if (!this.canFold()) return;
    this.done.emit();
    afterNextRender({ write: () => this.toggle()?.nativeElement.focus() }, { injector: this.injector });
  }
}
