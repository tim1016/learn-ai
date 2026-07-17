import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type { MarkdownDocument } from './markdown-document.model';
import { MarkdownDocumentCompiler } from './markdown-document-compiler.service';

@Injectable({ providedIn: 'root' })
export class MarkdownDocumentRepository {
  private readonly http = inject(HttpClient);
  private readonly compiler = inject(MarkdownDocumentCompiler);
  private readonly documents = new Map<string, Promise<MarkdownDocument>>();

  load(source: string): Promise<MarkdownDocument> {
    const cached = this.documents.get(source);
    if (cached !== undefined) return cached;

    const request = firstValueFrom(this.http.get(source, { responseType: 'text' }))
      .then(markdown => this.compiler.compile(markdown));

    this.documents.set(source, request);
    void request.catch(() => this.documents.delete(source));
    return request;
  }
}
