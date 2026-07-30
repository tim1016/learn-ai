import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, expect, it } from 'vitest';

import type { OfflineReplayStatus } from '../../../api/offline-replay.types';
import {
  OfflineReplayCommand,
  OfflineReplayTransportComponent,
} from './offline-replay-transport.component';

async function render(status: OfflineReplayStatus) {
  TestBed.resetTestingModule();
  await TestBed.configureTestingModule({
    imports: [OfflineReplayTransportComponent],
  }).compileComponents();
  const fixture = TestBed.createComponent(OfflineReplayTransportComponent);
  const commands: OfflineReplayCommand[] = [];
  fixture.componentRef.setInput('status', status);
  fixture.componentRef.setInput('speed', 60);
  fixture.componentRef.setInput('acting', false);
  fixture.componentInstance.replayCommand.subscribe((command) => commands.push(command));
  fixture.detectChanges();
  return { fixture, commands };
}

function click(
  fixture: ComponentFixture<OfflineReplayTransportComponent>,
  label: string,
): void {
  const root = fixture.nativeElement as HTMLElement;
  const button = Array.from(root.querySelectorAll('button')).find((element) =>
    element.textContent?.includes(label),
  );
  if (!button) throw new Error(`no button matching "${label}"`);
  button.dispatchEvent(new MouseEvent('click', { bubbles: true }));
}

describe('OfflineReplayTransportComponent', () => {
  it('emits a pause command while running', async () => {
    const { fixture, commands } = await render('running');

    click(fixture, 'Pause');

    expect(commands).toEqual([{ action: 'pause' }]);
    fixture.destroy();
  });

  it('offers resume and single-minute step while paused', async () => {
    const { fixture, commands } = await render('paused');

    click(fixture, 'Resume');
    click(fixture, 'Step one minute');

    expect(commands).toEqual([{ action: 'resume' }, { action: 'step' }]);
    fixture.destroy();
  });

  it('emits the chosen acceleration with a set_speed command', async () => {
    const { fixture, commands } = await render('running');

    click(fixture, '10×');

    expect(commands).toEqual([{ action: 'set_speed', speed: 10 }]);
    fixture.destroy();
  });
});
