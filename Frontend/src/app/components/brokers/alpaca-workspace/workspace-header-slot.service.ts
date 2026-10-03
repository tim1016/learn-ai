import { Injectable, signal, type TemplateRef } from '@angular/core';

/**
 * The one place a page under the account workspace puts its own controls in
 * the workspace header: a bot's page puts its name, figures and actions beside
 * Deploy a bot instead of in two rows of its own.
 *
 * Provided by the workspace, so a page rendered outside it finds no slot and
 * keeps its controls in its own body. The page hands over a template it
 * declared, so the controls keep the page's bindings and handlers, and takes
 * it back when it goes.
 */
@Injectable()
export class WorkspaceHeaderSlot {
  private readonly current = signal<TemplateRef<unknown> | null>(null);

  /** The open page's controls, or `null` when it has none to show. */
  readonly content = this.current.asReadonly();

  show(content: TemplateRef<unknown>): void {
    this.current.set(content);
  }

  /** Takes `content` back, and only `content`: a page arriving keeps the controls it just showed. */
  clear(content: TemplateRef<unknown>): void {
    if (this.current() === content) this.current.set(null);
  }
}
