import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  Injector,
  afterNextRender,
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
 * folded. The body stays in the page while folded (only hidden), so the
 * pickers and editors inside keep their state. Focus follows the owner:
 * Edit lands in the opened body, Done returns to the step's Edit button.
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
  readonly open = input.required<boolean>();
  readonly status = input<DeployStepStatus | null>(null);
  /** Offers Edit / Done. Money and Confirm are always open. */
  readonly foldable = input(false);
  /** Done waits for the step to be complete. */
  readonly canFold = input(true);
  /** Why Done is not offered yet. */
  readonly foldHint = input('');

  readonly edit = output();
  readonly done = output();

  private readonly injector = inject(Injector);
  private readonly id = nextStepId++;
  protected readonly headingId = `deploy-step-${this.id}-title`;
  protected readonly bodyId = `deploy-step-${this.id}-body`;
  protected readonly hintId = `deploy-step-${this.id}-hint`;

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
