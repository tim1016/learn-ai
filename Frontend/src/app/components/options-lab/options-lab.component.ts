import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';

import { BreadcrumbComponent } from '../../shared/breadcrumb/breadcrumb.component';

@Component({
  selector: 'app-options-lab',
  standalone: true,
  imports: [BreadcrumbComponent, RouterLink, RouterLinkActive, RouterOutlet],
  templateUrl: './options-lab.component.html',
  styleUrls: ['./options-lab.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class OptionsLabComponent {}
