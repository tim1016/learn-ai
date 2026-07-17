import { ChangeDetectionStrategy, Component, inject, resource, signal } from '@angular/core';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { toSignal } from '@angular/core/rxjs-interop';
import { map } from 'rxjs/operators';

import { MarkdownViewerComponent } from '../../../shared/markdown-viewer/markdown-viewer.component';
import { DocumentArticleComponent } from '../../../shared/markdown-document/document-article.component';
import { MarkdownDocumentRepository } from '../../../shared/markdown-document/markdown-document-repository.service';
import { SectionErrorComponent } from '../../../shared/errors/section-error.component';
import { OperatorManualHeroComponent } from './operator-manual-hero.component';
import { OperatorQuickProceduresComponent } from './operator-quick-procedures.component';

interface ManualChapter {
  readonly range: string;
  readonly title: string;
  readonly description: string;
  readonly anchor: string;
  readonly icon: string;
}

const MANUAL_CHAPTERS: readonly ManualChapter[] = [
  {
    range: '01–04',
    title: 'Know the system',
    description: 'Three planes, runtime topology, the Account Clerk, and reconciliation.',
    anchor: '1-mental-model-three-planes',
    icon: 'pi pi-sitemap',
  },
  {
    range: '05–06',
    title: 'Run the lifecycle',
    description: 'Roll call, start and stop semantics, admission gates, and submit gates.',
    anchor: '5-the-bot-lifecycle',
    icon: 'pi pi-sync',
  },
  {
    range: '07–08',
    title: 'Recover and scale',
    description: 'Freeze recovery, cure selection, and safe concurrent launches.',
    anchor: '7-freezes-recovery',
    icon: 'pi pi-shield',
  },
  {
    range: '09–10',
    title: 'Follow a procedure',
    description: 'Common operator recipes and symptom-to-remedy troubleshooting.',
    anchor: '9-common-operator-procedures',
    icon: 'pi pi-wrench',
  },
  {
    range: '11–12',
    title: 'Avoid the blindspots',
    description: 'The operational traps that bite, plus the system glossary.',
    anchor: '11-blindspots-the-things-that-bite',
    icon: 'pi pi-eye',
  },
];

@Component({
  selector: 'app-bot-operator-manual-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    MarkdownViewerComponent,
    DocumentArticleComponent,
    OperatorManualHeroComponent,
    OperatorQuickProceduresComponent,
    RouterLink,
    SectionErrorComponent,
  ],
  templateUrl: './bot-operator-manual-page.component.html',
  styleUrl: './bot-operator-manual-page.component.scss',
})
export class BotOperatorManualPageComponent {
  private readonly route = inject(ActivatedRoute);
  private readonly documents = inject(MarkdownDocumentRepository);

  readonly manualSource = '/assets/docs/bot-control-operator-manual.md';
  readonly chapters = MANUAL_CHAPTERS;
  readonly manualFragment = toSignal(this.route.fragment.pipe(map(fragment => fragment ?? null)), {
    initialValue: null,
  });
  readonly manual = resource({
    params: () => this.manualSource,
    loader: ({ params }) => this.documents.load(params),
  });
  readonly manualOpen = signal(false);

  setManualOpen(event: Event): void {
    if (event.currentTarget instanceof HTMLDetailsElement) {
      this.manualOpen.set(event.currentTarget.open);
    }
  }
}
