import { TestBed } from '@angular/core/testing';
import { ReplayControlsComponent } from './replay-controls.component';
import { ReplayEngineService } from '../../../services/replay-engine.service';
import { createMockAggregatesTimeSeries } from '../../../../testing/factories/market-data.factory';

describe('ReplayControlsComponent', () => {
  let component: ReplayControlsComponent;
  let fixture: ReturnType<typeof TestBed.createComponent<ReplayControlsComponent>>;
  let replayEngine: ReplayEngineService;

  beforeEach(async () => {
    vi.useFakeTimers();
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [ReplayControlsComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(ReplayControlsComponent);
    component = fixture.componentInstance;
    replayEngine = TestBed.inject(ReplayEngineService);
    fixture.detectChanges();
  });

  afterEach(() => {
    replayEngine.reset();
    vi.useRealTimers();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should show play button when stopped', () => {
    const bars = createMockAggregatesTimeSeries(5, 1);
    replayEngine.load(bars);
    fixture.detectChanges();

    const playBtn: HTMLButtonElement = fixture.nativeElement.querySelector('.play-btn');
    expect(playBtn.textContent!.trim()).toBe('\u25B6');
  });

  it('should show pause button when playing', () => {
    const bars = createMockAggregatesTimeSeries(10, 1);
    replayEngine.load(bars);
    replayEngine.play();
    fixture.detectChanges();

    const playBtn: HTMLButtonElement = fixture.nativeElement.querySelector('.play-btn');
    expect(playBtn.textContent!.trim()).toBe('\u23F8');
  });

  it('should toggle play/pause on button click', () => {
    const bars = createMockAggregatesTimeSeries(10, 1);
    replayEngine.load(bars);
    fixture.detectChanges();

    const playBtn: HTMLButtonElement = fixture.nativeElement.querySelector('.play-btn');

    playBtn.click();
    expect(replayEngine.playbackState()).toBe('playing');

    fixture.detectChanges();
    playBtn.click();
    expect(replayEngine.playbackState()).toBe('paused');
  });

  it('should step forward on step button click', () => {
    const bars = createMockAggregatesTimeSeries(5, 1);
    replayEngine.load(bars);
    fixture.detectChanges();

    const stepFwdBtns = fixture.nativeElement.querySelectorAll('.transport-btn');
    const stepFwdBtn = stepFwdBtns[3] as HTMLButtonElement; // 4th button is step forward
    stepFwdBtn.click();

    expect(replayEngine.currentIndex()).toBe(1);
  });

  it('should change speed when speed button clicked', () => {
    const speedBtns = fixture.nativeElement.querySelectorAll('.speed-btn');
    const fiveXBtn = speedBtns[2] as HTMLButtonElement; // [1x, 2x, 5x, ...]
    fiveXBtn.click();

    expect(replayEngine.playbackSpeed()).toBe(5);
  });

  it('should display bar counter with current position', () => {
    const bars = createMockAggregatesTimeSeries(100, 1);
    replayEngine.load(bars);
    replayEngine.seekTo(49);
    fixture.detectChanges();

    const counter: HTMLElement = fixture.nativeElement.querySelector('.bar-counter');
    expect(counter.textContent).toContain('Bar 50 / 100');
  });

  it('should disable step backward at start', () => {
    const bars = createMockAggregatesTimeSeries(5, 1);
    replayEngine.load(bars);
    fixture.detectChanges();

    const btns = fixture.nativeElement.querySelectorAll('.transport-btn');
    const stepBackBtn = btns[1] as HTMLButtonElement;
    expect(stepBackBtn.disabled).toBe(true);
  });

  it('should disable step forward at end', () => {
    const bars = createMockAggregatesTimeSeries(5, 1);
    replayEngine.load(bars);
    replayEngine.seekTo(4);
    fixture.detectChanges();

    const btns = fixture.nativeElement.querySelectorAll('.transport-btn');
    const stepFwdBtn = btns[3] as HTMLButtonElement;
    expect(stepFwdBtn.disabled).toBe(true);
  });
});
