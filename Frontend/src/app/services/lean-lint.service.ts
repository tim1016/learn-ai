import { HttpClient } from "@angular/common/http";
import { Injectable, inject } from "@angular/core";
import { Observable } from "rxjs";

import { environment } from "../../environments/environment";

/**
 * One ruff diagnostic, shape mirrors
 * ``PythonDataService/app/routers/lean_lint.py::_Diagnostic`` and spec
 * section 6.4.
 */
export interface Diagnostic {
  line: number;
  col: number;
  end_line: number | null;
  end_col: number | null;
  rule: string;
  severity: "warning" | "error" | "info";
  message: string;
  fix: string | null;
}

/**
 * HTTP client for ``POST /api/lean-sidecar/lint``.
 *
 * Returns an ``Observable`` (not a ``Promise``) so callers can compose
 * the lint stream with RxJS — ``LeanScriptEditorComponent`` pipes a
 * debounced source signal through ``switchMap`` and feeds the result
 * back to a signal via ``toSignal``.
 */
@Injectable({ providedIn: "root" })
export class LeanLintService {
  private readonly http = inject(HttpClient);
  private readonly url = `${environment.pythonServiceUrl}/api/lean-sidecar/lint`;

  lint(source: string): Observable<{ diagnostics: Diagnostic[] }> {
    return this.http.post<{ diagnostics: Diagnostic[] }>(this.url, { source });
  }
}
