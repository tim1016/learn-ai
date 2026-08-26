import {
  AfterViewInit,
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  OnDestroy,
  computed,
  effect,
  inject,
  input,
  model,
  resource,
  viewChild,
} from "@angular/core";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { python } from "@codemirror/lang-python";
import { defaultHighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { Compartment, EditorState, type Extension } from "@codemirror/state";
import { EditorView, keymap, lineNumbers } from "@codemirror/view";

import { LeanSourceService } from "../../../services/lean-source.service";
import type { LeanLauncherStatus } from "../strategy-lab.models";
import { CopyButtonComponent } from "../../../shared/copy-button/copy-button.component";

@Component({
  selector: "app-lean-source-editor",
  imports: [CopyButtonComponent],
  templateUrl: "./lean-source-editor.component.html",
  styleUrl: "./lean-source-editor.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LeanSourceEditorComponent implements AfterViewInit, OnDestroy {
  private readonly leanSource = inject(LeanSourceService);
  private readonly editorHost = viewChild.required<ElementRef<HTMLDivElement>>("editorHost");
  private readonly editability = new Compartment();
  private view: EditorView | null = null;
  private externalEditorWrite = false;

  readonly strategyName = input.required<string>();
  readonly launcherStatus = input.required<LeanLauncherStatus>();
  readonly customSource = model<string | null>(null);

  protected readonly sourceResource = resource({
    params: () => this.strategyName(),
    loader: ({ params }) => this.leanSource.getStrategySource(params),
  });
  protected readonly registeredSource = computed(() =>
    this.sourceResource.hasValue() ? this.sourceResource.value() : null,
  );
  protected readonly currentSource = computed(() =>
    this.customSource() ?? this.registeredSource()?.source ?? null,
  );
  protected readonly canEdit = computed(() => this.customSource() !== null);
  protected readonly runtimeLabel = computed(() => {
    switch (this.launcherStatus()) {
      case "ready": return "Runtime ready";
      case "checking": return "Checking runtime";
      case "blocked": return "Runtime not detected";
      default: return "Runtime undetected";
    }
  });
  protected readonly runtimeNotice = computed(() =>
    this.launcherStatus() === "ready"
      ? "LEAN is available. Enable custom source to execute the edited QCAlgorithm."
      : "LEAN runtime not detected. The QCAlgorithm remains available for viewing and editing; execution stays disabled until LEAN is ready.",
  );

  constructor() {
    effect(() => {
      const editable = this.canEdit();
      const view = this.view;
      if (view === null) return;
      view.dispatch({
        effects: this.editability.reconfigure(this.editabilityExtension(editable)),
      });
    });

    effect(() => {
      const next = this.currentSource() ?? "";
      const view = this.view;
      if (view === null || view.state.doc.toString() === next) return;
      this.externalEditorWrite = true;
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: next } });
      this.externalEditorWrite = false;
    });
  }

  ngAfterViewInit(): void {
    const extensions: Extension[] = [
      lineNumbers(),
      history(),
      keymap.of([...defaultKeymap, ...historyKeymap]),
      python(),
      syntaxHighlighting(defaultHighlightStyle),
      EditorView.lineWrapping,
      EditorView.contentAttributes.of({ "aria-label": "QCAlgorithm source editor" }),
      this.editability.of(this.editabilityExtension(this.canEdit())),
      EditorView.updateListener.of((update) => {
        if (!update.docChanged || this.externalEditorWrite) return;
        this.customSource.set(update.state.doc.toString());
      }),
    ];
    this.view = new EditorView({
      state: EditorState.create({ doc: this.currentSource() ?? "", extensions }),
      parent: this.editorHost().nativeElement,
    });
  }

  ngOnDestroy(): void {
    this.view?.destroy();
    this.view = null;
  }

  protected toggleCustomSource(event: Event): void {
    const target = event.target;
    if (!(target instanceof HTMLInputElement)) return;
    if (!target.checked) {
      this.customSource.set(null);
      return;
    }
    const registered = this.registeredSource();
    if (registered !== null) this.customSource.set(registered.source);
  }

  protected resetSource(): void {
    const registered = this.registeredSource();
    if (registered !== null && this.canEdit()) this.customSource.set(registered.source);
  }

  private editabilityExtension(editable: boolean): Extension {
    return [
      EditorState.readOnly.of(!editable),
      EditorView.editable.of(editable),
    ];
  }
}
