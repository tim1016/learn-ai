import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { StockAnalysisComponent, generateMonthlyChunks } from './stock-analysis.component';

describe('StockAnalysisComponent', () => {
  let component: StockAnalysisComponent;
  let fixture: ComponentFixture<StockAnalysisComponent>;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [StockAnalysisComponent],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();

    fixture = TestBed.createComponent(StockAnalysisComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should have correct default signal values', () => {
    expect(component.ticker()).toBe('GLD');
    expect(component.chunkDelayMs()).toBe(12000);
    expect(component.isRunning()).toBe(false);
    expect(component.abortRequested()).toBe(false);
    expect(component.forceRefresh()).toBe(false);
    expect(component.selectedChunk()).toBeNull();
  });

  it('should set fromDate 2 years ago and toDate today', () => {
    const from = component.fromDate();
    const to = component.toDate();
    expect(from).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(to).toMatch(/^\d{4}-\d{2}-\d{2}$/);

    const fromDate = new Date(from);
    const toDate = new Date(to);
    const diffYears = (toDate.getTime() - fromDate.getTime()) / (365.25 * 24 * 60 * 60 * 1000);
    expect(diffYears).toBeCloseTo(2, 0);
  });

  it('should have empty chunks and aggregates by default', () => {
    expect(component.chunks()).toEqual([]);
    expect(component.allAggregates()).toEqual([]);
    expect(component.sortedAggregates()).toEqual([]);
  });

  it('should compute canStart as true when ticker is set and not running', () => {
    expect(component.canStart()).toBe(true);
  });

  it('should compute canStart as false when ticker is empty', () => {
    component.ticker.set('');
    expect(component.canStart()).toBe(false);
  });

  it('should compute canStart as false when running', () => {
    component.isRunning.set(true);
    expect(component.canStart()).toBe(false);
  });

  it('should not start analysis when ticker is empty', () => {
    component.ticker.set('  ');
    component.startAnalysis();
    expect(component.chunks()).toEqual([]);
    expect(component.isRunning()).toBe(false);
  });

  it('should set abortRequested when stopAnalysis is called', () => {
    component.stopAnalysis();
    expect(component.abortRequested()).toBe(true);
  });

  it('should compute progressPercent as 0 when no chunks', () => {
    expect(component.progressPercent()).toBe(0);
  });
});

describe('generateMonthlyChunks', () => {
  it('should generate correct number of chunks for a 2-year range', () => {
    const chunks = generateMonthlyChunks('2024-02-14', '2026-02-14');
    // Feb 2024 (partial) + Mar-Dec 2024 (10) + Jan-Dec 2025 (12) + Jan-Feb 2026 (partial) ≈ 24-25
    expect(chunks.length).toBeGreaterThanOrEqual(24);
    expect(chunks.length).toBeLessThanOrEqual(25);
  });

  it('should generate a single chunk for a partial month', () => {
    const chunks = generateMonthlyChunks('2024-03-15', '2024-03-25');
    expect(chunks.length).toBe(1);
    expect(chunks[0].fromDate).toBe('2024-03-15');
    expect(chunks[0].toDate).toBe('2024-03-25');
  });

  it('should generate 2 chunks spanning a month boundary', () => {
    const chunks = generateMonthlyChunks('2024-03-15', '2024-04-15');
    expect(chunks.length).toBe(2);
    expect(chunks[0].fromDate).toBe('2024-03-15');
    expect(chunks[0].toDate).toBe('2024-03-31');
    expect(chunks[1].fromDate).toBe('2024-04-01');
    expect(chunks[1].toDate).toBe('2024-04-15');
  });

  it('should generate chunks with correct indexes', () => {
    const chunks = generateMonthlyChunks('2024-01-01', '2024-04-30');
    expect(chunks.map(c => c.index)).toEqual([0, 1, 2, 3]);
  });

  it('should generate chunks with pending status and zero counts', () => {
    const chunks = generateMonthlyChunks('2024-01-01', '2024-02-28');
    for (const chunk of chunks) {
      expect(chunk.status).toBe('pending');
      expect(chunk.barCount).toBe(0);
      expect(chunk.durationMs).toBe(0);
    }
  });

  it('should handle full months correctly', () => {
    const chunks = generateMonthlyChunks('2024-01-01', '2024-03-31');
    expect(chunks.length).toBe(3);
    expect(chunks[0].fromDate).toBe('2024-01-01');
    expect(chunks[0].toDate).toBe('2024-01-31');
    expect(chunks[1].fromDate).toBe('2024-02-01');
    expect(chunks[1].toDate).toBe('2024-02-29'); // 2024 is a leap year
    expect(chunks[2].fromDate).toBe('2024-03-01');
    expect(chunks[2].toDate).toBe('2024-03-31');
  });
});
