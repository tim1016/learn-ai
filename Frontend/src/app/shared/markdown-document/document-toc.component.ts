import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { DocumentSection } from './markdown-document.model';

@Component({
  selector: 'app-document-toc',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  templateUrl: './document-toc.component.html',
  styleUrl: './document-toc.component.scss',
})
export class DocumentTocComponent {
  readonly sections = input.required<readonly DocumentSection[]>();
  readonly route = input.required<string>();
  readonly activeSection = input<string | null>(null);
}
