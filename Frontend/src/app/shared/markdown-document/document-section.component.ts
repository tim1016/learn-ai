import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import { DocumentBlockComponent } from './document-block.component';
import type { DocumentSection } from './markdown-document.model';

@Component({
  selector: 'app-document-section',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DocumentBlockComponent, RouterLink],
  templateUrl: './document-section.component.html',
  styleUrl: './document-section.component.scss',
})
export class DocumentSectionComponent {
  readonly section = input.required<DocumentSection>();
  readonly route = input.required<string>();
}
