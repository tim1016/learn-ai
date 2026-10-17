import { ChangeDetectionStrategy, Component, computed, input, output, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import type { ManualChapter } from './trader-manual';

@Component({
  selector: 'app-manual-contents',
  imports: [RouterLink],
  templateUrl: './manual-contents.component.html',
  styleUrl: './manual-contents.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ManualContentsComponent {
  readonly chapters = input.required<readonly ManualChapter[]>();
  readonly route = input.required<string>();
  readonly active = input<string | null>(null);
  readonly chapterPicked = output<string>();
  protected readonly query = signal('');
  protected readonly groups = computed(() => {
    const words = this.query().toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
    const chapters = this.chapters().map(chapter => {
      const text = `${chapter.title} ${chapter.description} ${chapter.group}`.toLocaleLowerCase();
      return { ...chapter, matches: words.every(word => text.includes(word)) };
    });
    return [...new Set(chapters.map(chapter => chapter.group))].map(title => ({
      title, chapters: chapters.filter(chapter => chapter.group === title),
      visible: chapters.some(chapter => chapter.group === title && chapter.matches),
    }));
  });
  protected readonly noMatches = computed(() => this.groups().every(group => !group.visible));
}
