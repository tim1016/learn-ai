import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { DocumentBlock } from './markdown-document.model';

@Component({
  selector: 'app-document-block',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  templateUrl: './document-block.component.html',
  styleUrl: './document-block.component.scss',
})
export class DocumentBlockComponent {
  readonly block = input.required<DocumentBlock>();
  readonly route = input.required<string>();
}
