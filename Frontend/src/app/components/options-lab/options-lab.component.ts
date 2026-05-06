import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';

@Component({
  selector: 'app-options-lab',
  standalone: true,
  imports: [RouterLink, RouterLinkActive, RouterOutlet],
  templateUrl: './options-lab.component.html',
  styleUrls: ['./options-lab.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class OptionsLabComponent {}
