import { TestBed } from '@angular/core/testing';
import { ReplayEngineService } from './replay-engine.service';
import {
  createMockAggregatesTimeSeries,
  createMockAggregate,
} from '../../testing/factories/market-data.factory';

describe('ReplayEngineService', () => {
  let service: ReplayEngineService;

  beforeEach(() => {
    vi.useFakeTimers();
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
    service = TestBed.inject(ReplayEngineService);
  });

  afterEach(() => {
    service.reset();
    vi.useRealTimers();
  });

  describe('load', () => {
    it('should sort bars by timestamp and reset index to 0', () => {
      const bars = [
        createMockAggregate({ id: 3, timestamp: '2026-01-03T00:00:00Z' }),
        createMockAggregate({ id: 1, timestamp: '2026-01-01T00:00:00Z' }),
        createMockAggregate({ id: 2, timestamp: '2026-01-02T00:00:00Z' }),
      ];

      service.load(bars);

      expect(service.totalBars()).toBe(3);
      expect(service.currentIndex()).toBe(0);
      expect(service.playbackState()).toBe('stopped');
      expect(service.bars()[0].id).toBe(1);
      expect(service.bars()[1].id).toBe(2);
      expect(service.bars()[2].id).toBe(3);
    });

    it('should handle empty array', () => {
      service.load([]);

      expect(service.totalBars()).toBe(0);
      expect(service.currentIndex()).toBe(0);
      expect(service.currentBar()).toBeNull();
    });
  });

  describe('stepForward', () => {
    it('should increment index by 1 and grow visibleBars', () => {
      const bars = createMockAggregatesTimeSeries(5, 1);
      service.load(bars);

      expect(service.visibleBars().length).toBe(1);

      service.stepForward();
      expect(service.currentIndex()).toBe(1);
      expect(service.visibleBars().length).toBe(2);

      service.stepForward();
      expect(service.currentIndex()).toBe(2);
      expect(service.visibleBars().length).toBe(3);
    });

    it('should stay at last index when already at end', () => {
      const bars = createMockAggregatesTimeSeries(3, 1);
      service.load(bars);

      service.seekTo(2);
      expect(service.isAtEnd()).toBe(true);

      service.stepForward();
      expect(service.currentIndex()).toBe(2);
      expect(service.visibleBars().length).toBe(3);
    });

    it('should do nothing when no data loaded', () => {
      service.stepForward();
      expect(service.currentIndex()).toBe(0);
    });
  });

  describe('stepBackward', () => {
    it('should decrement index and shrink visibleBars', () => {
      const bars = createMockAggregatesTimeSeries(5, 1);
      service.load(bars);
      service.seekTo(3);

      expect(service.visibleBars().length).toBe(4);

      service.stepBackward();
      expect(service.currentIndex()).toBe(2);
      expect(service.visibleBars().length).toBe(3);
    });

    it('should stay at 0 when already at start', () => {
      const bars = createMockAggregatesTimeSeries(5, 1);
      service.load(bars);

      expect(service.isAtStart()).toBe(true);

      service.stepBackward();
      expect(service.currentIndex()).toBe(0);
    });
  });

  describe('seekTo', () => {
    it('should set index to exact value', () => {
      const bars = createMockAggregatesTimeSeries(100, 1);
      service.load(bars);

      service.seekTo(50);
      expect(service.currentIndex()).toBe(50);
      expect(service.visibleBars().length).toBe(51);
    });

    it('should clamp to valid range', () => {
      const bars = createMockAggregatesTimeSeries(10, 1);
      service.load(bars);

      service.seekTo(100);
      expect(service.currentIndex()).toBe(9);

      service.seekTo(-5);
      expect(service.currentIndex()).toBe(0);
    });
  });

  describe('seekToPercent', () => {
    it('should jump to midpoint at 50%', () => {
      const bars = createMockAggregatesTimeSeries(101, 1);
      service.load(bars);

      service.seekToPercent(0.5);
      expect(service.currentIndex()).toBe(50);
    });

    it('should handle 0% and 100%', () => {
      const bars = createMockAggregatesTimeSeries(100, 1);
      service.load(bars);

      service.seekToPercent(0);
      expect(service.currentIndex()).toBe(0);

      service.seekToPercent(1);
      expect(service.currentIndex()).toBe(99);
    });

    it('should clamp values outside 0-1', () => {
      const bars = createMockAggregatesTimeSeries(50, 1);
      service.load(bars);

      service.seekToPercent(2.0);
      expect(service.currentIndex()).toBe(49);

      service.seekToPercent(-0.5);
      expect(service.currentIndex()).toBe(0);
    });
  });

  describe('no-lookahead guarantee', () => {
    it('visibleBars at index N contains exactly bars[0..N]', () => {
      const bars = createMockAggregatesTimeSeries(20, 1);
      service.load(bars);

      for (let n = 0; n < 20; n++) {
        service.seekTo(n);
        const visible = service.visibleBars();

        expect(visible.length).toBe(n + 1);
        expect(visible[visible.length - 1].timestamp).toBe(service.bars()[n].timestamp);

        // Verify each visible bar matches the expected bar
        for (let j = 0; j <= n; j++) {
          expect(visible[j].timestamp).toBe(service.bars()[j].timestamp);
        }
      }
    });

    it('future bars are never accessible via any public computed signal', () => {
      const bars = createMockAggregatesTimeSeries(10, 1);
      service.load(bars);

      service.seekTo(5);
      const visible = service.visibleBars();
      const currentBar = service.currentBar();
      const currentTimestamp = new Date(currentBar!.timestamp).getTime();

      // No visible bar should have a timestamp beyond the current bar
      for (const bar of visible) {
        expect(new Date(bar.timestamp).getTime()).toBeLessThanOrEqual(currentTimestamp);
      }

      // visibleBars should NOT contain any bar from index 6+
      expect(visible.length).toBe(6);
    });
  });

  describe('determinism', () => {
    it('same data produces identical visibleBars sequence across runs', () => {
      const bars = createMockAggregatesTimeSeries(50, 1);

      // Run 1
      service.load(bars);
      const sequence1: number[] = [];
      while (!service.isAtEnd()) {
        service.stepForward();
        sequence1.push(service.visibleBars().length);
      }

      // Run 2 (same data)
      service.load(bars);
      const sequence2: number[] = [];
      while (!service.isAtEnd()) {
        service.stepForward();
        sequence2.push(service.visibleBars().length);
      }

      expect(sequence1).toEqual(sequence2);
      expect(sequence1.length).toBe(49); // 49 stepForward calls from index 0 to 49
    });
  });

  describe('play/pause/stop lifecycle', () => {
    it('play should start ticking and advance index', () => {
      const bars = createMockAggregatesTimeSeries(10, 1);
      service.load(bars);

      service.play();
      expect(service.playbackState()).toBe('playing');

      vi.advanceTimersByTime(100); // 1 tick at default speed (100ms)
      expect(service.currentIndex()).toBe(1);

      vi.advanceTimersByTime(100);
      expect(service.currentIndex()).toBe(2);

      service.pause();
    });

    it('pause should stop ticking but preserve index', () => {
      const bars = createMockAggregatesTimeSeries(10, 1);
      service.load(bars);

      service.play();
      vi.advanceTimersByTime(200);
      expect(service.currentIndex()).toBe(2);

      service.pause();
      expect(service.playbackState()).toBe('paused');
      expect(service.currentIndex()).toBe(2);

      vi.advanceTimersByTime(500);
      expect(service.currentIndex()).toBe(2); // No advancement after pause
    });

    it('stop should reset index to 0', () => {
      const bars = createMockAggregatesTimeSeries(10, 1);
      service.load(bars);

      service.play();
      vi.advanceTimersByTime(300);
      expect(service.currentIndex()).toBe(3);

      service.stop();
      expect(service.playbackState()).toBe('stopped');
      expect(service.currentIndex()).toBe(0);
    });

    it('play should not start when no data loaded', () => {
      service.play();
      expect(service.playbackState()).toBe('stopped');
    });

    it('play should not start when already at end', () => {
      const bars = createMockAggregatesTimeSeries(3, 1);
      service.load(bars);
      service.seekTo(2);

      service.play();
      expect(service.playbackState()).toBe('stopped');
    });

    it('should auto-pause when reaching end during playback', () => {
      const bars = createMockAggregatesTimeSeries(3, 1);
      service.load(bars);

      service.play();
      vi.advanceTimersByTime(100); // index 1
      vi.advanceTimersByTime(100); // index 2 (end) → auto-pause

      expect(service.currentIndex()).toBe(2);
      expect(service.playbackState()).toBe('paused');
    });
  });

  describe('speed control', () => {
    it('should adjust playback interval when speed changes', () => {
      const bars = createMockAggregatesTimeSeries(20, 1);
      service.load(bars);

      service.setSpeed(2); // 100ms / 2 = 50ms per tick
      service.play();

      vi.advanceTimersByTime(50);
      expect(service.currentIndex()).toBe(1);

      vi.advanceTimersByTime(50);
      expect(service.currentIndex()).toBe(2);

      service.pause();
    });

    it('should update interval during active playback', () => {
      const bars = createMockAggregatesTimeSeries(20, 1);
      service.load(bars);

      service.play(); // 1x speed = 100ms/tick
      vi.advanceTimersByTime(100);
      expect(service.currentIndex()).toBe(1);

      service.setSpeed(5); // 100ms / 5 = 20ms/tick
      vi.advanceTimersByTime(20);
      expect(service.currentIndex()).toBe(2);
      vi.advanceTimersByTime(20);
      expect(service.currentIndex()).toBe(3);

      service.pause();
    });

    it('should ignore invalid speed values', () => {
      service.setSpeed(0);
      expect(service.playbackSpeed()).toBe(1); // unchanged

      service.setSpeed(-5);
      expect(service.playbackSpeed()).toBe(1); // unchanged
    });
  });

  describe('progress signal', () => {
    it('should be 0 at start and 1 at end', () => {
      const bars = createMockAggregatesTimeSeries(10, 1);
      service.load(bars);

      expect(service.progress()).toBe(0);

      service.seekTo(9);
      expect(service.progress()).toBe(1);
    });

    it('should be 0 when no data loaded', () => {
      expect(service.progress()).toBe(0);
    });

    it('should be proportional to position', () => {
      const bars = createMockAggregatesTimeSeries(11, 1);
      service.load(bars);

      service.seekTo(5);
      expect(service.progress()).toBeCloseTo(0.5, 5);
    });
  });

  describe('currentBar', () => {
    it('should be null when no data loaded', () => {
      expect(service.currentBar()).toBeNull();
    });

    it('should return the bar at the current index', () => {
      const bars = createMockAggregatesTimeSeries(5, 1);
      service.load(bars);

      service.seekTo(3);
      expect(service.currentBar()!.timestamp).toBe(service.bars()[3].timestamp);
    });
  });

  describe('reset', () => {
    it('should clear all state', () => {
      const bars = createMockAggregatesTimeSeries(10, 1);
      service.load(bars);
      service.seekTo(5);
      service.setSpeed(10);

      service.reset();

      expect(service.totalBars()).toBe(0);
      expect(service.currentIndex()).toBe(0);
      expect(service.playbackState()).toBe('stopped');
      expect(service.playbackSpeed()).toBe(1);
      expect(service.currentBar()).toBeNull();
      expect(service.visibleBars()).toEqual([]);
    });
  });
});
