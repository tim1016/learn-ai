import { ChangeDetectionStrategy, Component, OnDestroy, OnInit } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';

@Component({
  selector: 'app-options-lab',
  standalone: true,
  imports: [RouterLink, RouterLinkActive, RouterOutlet],
  templateUrl: './options-lab.component.html',
  styleUrls: ['./options-lab.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class OptionsLabComponent implements OnInit, OnDestroy {
  ngOnInit(): void {
    document.documentElement.classList.add('app-dark');
  }

  ngOnDestroy(): void {
    document.documentElement.classList.remove('app-dark');
  }
}
