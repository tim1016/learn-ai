import { ChangeDetectionStrategy, Component } from '@angular/core';

import { PageHeaderComponent } from '../../../shared/page-header/page-header.component';

@Component({
  selector: 'app-legal-notices-page',
  imports: [PageHeaderComponent],
  templateUrl: './legal-notices-page.component.html',
  styleUrl: './legal-notices-page.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LegalNoticesPageComponent {}
