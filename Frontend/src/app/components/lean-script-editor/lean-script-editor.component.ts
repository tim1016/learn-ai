import { CommonModule } from "@angular/common";
import {
  AfterViewInit,
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  OnDestroy,
  computed,
  effect,
  inject,
  model,
  signal,
  viewChild,
} from "@angular/core";
import { toObservable, toSignal } from "@angular/core/rxjs-interop";
import {
  catchError,
  debounceTime,
  distinctUntilChanged,
  of,
  switchMap,
} from "rxjs";

import { python } from "@codemirror/lang-python";
import { EditorState, type Extension } from "@codemirror/state";
import { EditorView, keymap, lineNumbers } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { syntaxHighlighting, defaultHighlightStyle } from "@codemirror/language";

import { LeanLintService, type Diagnostic } from "../../services/lean-lint.service";
import { EMA_CROSSOVER_SOURCE_TEMPLATE } from "./lean-script-editor.template";

/**
 * The lint endpoint debounces below this; the editor mirrors the
 * server-side timeout so callers see a quiet pause rather than a busy
 * spinner while ruff is mid-flight.
 */
const LINT_DEBOUNCE_MS = 500;

/**
 * In-page Python editor for the unified Engine Lab's LEAN script
 * surface. Hosts a CodeMirror 6 instance and round-trips its content
 * through a ``source`` model signal so parents can two-way bind:
 *
 * ```html
 * <app-lean-script-editor [(source)]="leanSource" />
 * ```
 *
 * Lint pipeline: the source signal drives a 500ms-debounced observable
 * that calls ``LeanLintService.lint`` and renders the response in a
 * Problems panel below the editor. Clicking a diagnostic scrolls the
 * CodeMirror view to the offending line so the operator can fix it
 * without manual scrolling.
 *
 * Default content (``EMA_CROSSOVER_SOURCE_TEMPLATE``) is a skeleton
 * matching the trusted-sample's structure — drift with the sample is
 * acceptable because the sample is a parity oracle, while this string
 * is a starter for operator edits.
 */
@Component({
  selector: "app-lean-script-editor",
  standalone: true,
  imports: [CommonModule],
  templateUrl: "./lean-script-editor.component.html",
  styleUrls: ["./lean-script-editor.component.scss"],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LeanScriptEditorComponent implements AfterViewInit, OnDestroy {
  private readonly lintService = inject(LeanLintService);

  /**
   * Two-way bindable source. Seeded with the EMA-crossover template on
   * first construction; parents may overwrite at any time.
   */
  readonly source = model<string>(EMA_CROSSOVER_SOURCE_TEMPLATE);

  private readonly editorHost =
    viewChild.required<ElementRef<HTMLDivElement>>("editorHost");

  /**
   * The diagnostics signal — populated by the lint pipeline below.
   * Empty array means a clean source or an in-flight request.
   */
  readonly diagnostics = signal<Diagnostic[]>([]);

  /** True while the lint observable is mid-flight. */
  readonly linting = signal<boolean>(false);

  readonly warningCount = computed(
    () => this.diagnostics().filter((d) => d.severity === "warning").length,
  );
  readonly infoCount = computed(
    () => this.diagnostics().filter((d) => d.severity === "info").length,
  );

  private view: EditorView | null = null;
  /**
   * Guard against the round-trip where (a) the user types into the
   * editor, (b) we set ``source``, (c) the ``effect`` below sees the
   * new value and tries to push it back into the editor — corrupting
   * the cursor position.
   */
  private suppressEditorWrite = false;

  constructor() {
    // Wire the lint pipeline: source signal -> observable -> debounce -> lint.
    const lintResult = toSignal(
      toObservable(this.source).pipe(
        debounceTime(LINT_DEBOUNCE_MS),
        distinctUntilChanged(),
        switchMap((source) => {
          this.linting.set(true);
          return this.lintService.lint(source).pipe(
            catchError(() => of({ diagnostics: [] as Diagnostic[] })),
          );
        }),
      ),
      { initialValue: { diagnostics: [] as Diagnostic[] } },
    );

    effect(() => {
      this.diagnostics.set(lintResult().diagnostics);
      this.linting.set(false);
    });

    // When the model signal changes from outside (parent set, or default
    // re-applied), push the new value into the CodeMirror view.
    effect(() => {
      const next = this.source();
      if (this.suppressEditorWrite) return;
      const view = this.view;
      if (!view) return;
      const current = view.state.doc.toString();
      if (current === next) return;
      view.dispatch({
        changes: { from: 0, to: current.length, insert: next },
      });
    });
  }

  ngAfterViewInit(): void {
    const host = this.editorHost().nativeElement;
    const extensions: Extension[] = [
      lineNumbers(),
      history(),
      keymap.of([...defaultKeymap, ...historyKeymap]),
      python(),
      syntaxHighlighting(defaultHighlightStyle),
      EditorView.lineWrapping,
      EditorView.updateListener.of((update) => {
        if (!update.docChanged) return;
        const next = update.state.doc.toString();
        this.suppressEditorWrite = true;
        this.source.set(next);
        this.suppressEditorWrite = false;
      }),
    ];

    this.view = new EditorView({
      state: EditorState.create({
        doc: this.source(),
        extensions,
      }),
      parent: host,
    });
  }

  ngOnDestroy(): void {
    this.view?.destroy();
    this.view = null;
  }

  /**
   * Scroll the CodeMirror view so the requested 1-based line is in
   * view. Public so the component spec can swap it for a spy.
   */
  scrollEditorToLine(line: number): void {
    const view = this.view;
    if (!view) return;
    const doc = view.state.doc;
    const safeLine = Math.max(1, Math.min(line, doc.lines));
    const pos = doc.line(safeLine).from;
    view.dispatch({
      selection: { anchor: pos },
      effects: EditorView.scrollIntoView(pos, { y: "center" }),
    });
    view.focus();
  }

  onDiagnosticClick(diagnostic: Diagnostic): void {
    this.scrollEditorToLine(diagnostic.line);
  }

  /** Stable track-by for the Problems panel. */
  trackDiagnostic(index: number, d: Diagnostic): string {
    return `${d.line}:${d.col}:${d.rule}:${index}`;
  }
}
