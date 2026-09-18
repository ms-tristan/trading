import { describe, expect, it } from 'vitest';

import {
  buildSeries,
  chartValues,
  formatAxisValue,
  nearestIndex,
  niceScale,
  type SeriesKey,
} from './chart';
import type { EquityPoint } from './types';

/** One equity point; the two other series are `null` unless overridden. */
function point(
  timestamp: string,
  equity: number | null,
  cash: number | null = null,
  positionValue: number | null = null,
): EquityPoint {
  return { timestamp, equity, cash, position_value: positionValue };
}

const POINTS: EquityPoint[] = [
  point('2024-01-01T00:00:00+00:00', 0),
  point('2024-01-01T01:00:00+00:00', 10),
  point('2024-01-01T02:00:00+00:00', 20),
];

/** Count the occurrences of one SVG path command letter. */
function countCommand(path: string, letter: string): number {
  return path.split(letter).length - 1;
}

describe('chartValues', () => {
  it('collects every finite value of the three series', () => {
    const points = [point('t0', 1, 2, null), point('t1', null, 3, 4)];

    expect(chartValues(points)).toEqual([1, 2, 3, 4]);
  });

  it('ignores null values and an empty payload', () => {
    expect(chartValues([])).toEqual([]);
    expect(chartValues([point('t0', null, null, null)])).toEqual([]);
  });
});

describe('niceScale', () => {
  it('builds an ascending, evenly spaced domain for a normal series', () => {
    const scale = niceScale([0, 10, 20]);

    expect(scale.min).toBe(0);
    expect(scale.max).toBe(20);
    expect(scale.ticks).toEqual([0, 10, 20]);
    expect(scale.max).toBeGreaterThan(scale.min);
  });

  it('never returns a zero-height domain for a flat series', () => {
    const scale = niceScale([10000, 10000, 10000]);

    expect(scale.max).toBeGreaterThan(scale.min);
    expect(scale.min).toBeLessThanOrEqual(10000);
    expect(scale.max).toBeGreaterThanOrEqual(10000);
    expect(scale.ticks.length).toBeGreaterThanOrEqual(2);
    for (const tick of scale.ticks) {
      expect(Number.isFinite(tick)).toBe(true);
    }
  });

  it('never returns a zero-height domain for a single value', () => {
    const scale = niceScale([42]);

    expect(scale.max).toBeGreaterThan(scale.min);
    expect(scale.min).toBeLessThanOrEqual(42);
    expect(scale.max).toBeGreaterThanOrEqual(42);
  });

  it('never divides by zero for a single zero value', () => {
    const scale = niceScale([0]);

    expect(scale.min).toBeLessThan(scale.max);
    expect(scale.ticks[0]).toBe(scale.min);
    expect(scale.ticks[scale.ticks.length - 1]).toBe(scale.max);
  });

  it('falls back to a neutral domain when every value is missing', () => {
    expect(niceScale([])).toEqual({ min: 0, max: 1, ticks: [0, 1] });
    expect(niceScale([null, null])).toEqual({ min: 0, max: 1, ticks: [0, 1] });
  });

  it('rejects an unusable tick count instead of producing a broken axis', () => {
    const scale = niceScale([0, 100], 0);

    expect(scale.max).toBeGreaterThan(scale.min);
    expect(scale.ticks.length).toBeGreaterThanOrEqual(2);
  });

  it('spaces its ticks evenly and covers the whole domain', () => {
    const scale = niceScale([1000, 5000, 12345.67], 5);
    const steps = scale.ticks.slice(1).map((tick, index) => tick - (scale.ticks[index] ?? 0));

    expect(scale.ticks[0]).toBe(scale.min);
    expect(scale.ticks[scale.ticks.length - 1]).toBe(scale.max);
    for (const step of steps) {
      expect(step).toBeCloseTo(steps[0] ?? 0, 6);
    }
  });
});

describe('buildSeries', () => {
  it('projects a known input onto the exact geometry of the box', () => {
    const series = buildSeries(POINTS, 'equity', 100, 100, 0);

    expect(series.path).toBe('M0 100 L50 50 L100 0');
    expect(series.coords).toEqual([
      { x: 0, y: 100 },
      { x: 50, y: 50 },
      { x: 100, y: 0 },
    ]);
  });

  it('honours the padding on every side', () => {
    const series = buildSeries(POINTS, 'equity', 100, 100, 10);

    expect(series.coords[0]).toEqual({ x: 10, y: 90 });
    expect(series.coords[1]).toEqual({ x: 50, y: 50 });
    expect(series.coords[2]).toEqual({ x: 90, y: 10 });
  });

  it('centres a single point', () => {
    const series = buildSeries([point('t0', 5)], 'equity', 100, 100, 10);

    expect(series.coords).toEqual([{ x: 50, y: 50 }]);
    expect(series.path).toBe('M50 50');
  });

  it('starts a new subpath at a null value instead of dropping to zero', () => {
    const points = [
      point('t0', 10),
      point('t1', null),
      point('t2', 30),
      point('t3', 40),
    ];
    const series = buildSeries(points, 'equity', 100, 100, 0);

    expect(countCommand(series.path, 'M')).toBe(2);
    expect(countCommand(series.path, 'L')).toBe(1);
    expect(series.path).toBe('M0 100 M66.667 33.333 L100 0');
    expect(series.coords).toHaveLength(4);
    expect(Number.isNaN(series.coords[1]?.y)).toBe(true);
    // The gap is a gap: no coordinate of the series ever sits on the zero line
    // of the drawable box because of a missing value.
    expect(series.path).not.toContain('M33.333 100');
  });

  it('splits the three series over one shared y-domain', () => {
    const points = [point('t0', 10, 100, 0), point('t1', 30, 200, 0)];
    const equity = buildSeries(points, 'equity', 100, 100, 0);
    const cash = buildSeries(points, 'cash', 100, 100, 0);

    // Both series share one x geometry and one y-domain of 0 -> 200.
    expect(equity.coords[0]?.x).toBe(cash.coords[0]?.x);
    expect(equity.coords[1]?.x).toBe(cash.coords[1]?.x);
    // cash (100 -> 200) sits in the middle of that domain and ends on the top.
    expect(cash.coords[0]).toEqual({ x: 0, y: 50 });
    expect(cash.coords[1]).toEqual({ x: 100, y: 0 });
    // equity (10 -> 30) stays near the bottom of the very same domain.
    expect(equity.coords[0]).toEqual({ x: 0, y: 95 });
    expect(equity.coords[1]).toEqual({ x: 100, y: 85 });
  });

  it('returns an empty path when the series holds no value', () => {
    const points = [point('t0', null), point('t1', null)];
    const series = buildSeries(points, 'equity', 100, 100, 0);

    expect(series.path).toBe('');
    expect(series.coords).toHaveLength(2);
    expect(series.coords.every((coord) => Number.isNaN(coord.y))).toBe(true);
  });

  it('survives an unusable box instead of emitting NaN coordinates', () => {
    const series = buildSeries(POINTS, 'equity' as SeriesKey, 0, 0, -10);

    for (const coord of series.coords) {
      expect(Number.isFinite(coord.x)).toBe(true);
      expect(Number.isFinite(coord.y)).toBe(true);
    }
    expect(series.path).not.toContain('NaN');
  });

  it('builds one path per series key', () => {
    const points = [point('t0', 1, 2, 3), point('t1', 2, 3, 4)];

    for (const key of ['equity', 'cash', 'position_value'] as const) {
      const series = buildSeries(points, key, 800, 320, 56);
      expect(series.path.startsWith('M')).toBe(true);
      expect(series.coords).toHaveLength(2);
    }
  });
});

describe('nearestIndex', () => {
  const coords = [
    { x: 0, y: 10 },
    { x: 50, y: 20 },
    { x: 100, y: 30 },
  ];

  it('returns the first index for a cursor at or before the left end', () => {
    expect(nearestIndex(coords, 0)).toBe(0);
    expect(nearestIndex(coords, -1000)).toBe(0);
  });

  it('returns the last index for a cursor at or after the right end', () => {
    expect(nearestIndex(coords, 100)).toBe(2);
    expect(nearestIndex(coords, 1_000_000)).toBe(2);
  });

  it('returns the closest index in the middle and resolves ties downwards', () => {
    expect(nearestIndex(coords, 74)).toBe(1);
    expect(nearestIndex(coords, 76)).toBe(2);
    expect(nearestIndex(coords, 25)).toBe(0);
  });

  it('returns -1 for an empty coordinate list', () => {
    expect(nearestIndex([], 10)).toBe(-1);
  });
});

describe('formatAxisValue', () => {
  it('groups thousands without decimals', () => {
    expect(formatAxisValue(10500)).toBe('10,500');
    expect(formatAxisValue(-2500)).toBe('-2,500');
  });

  it('keeps a useful precision for small magnitudes', () => {
    expect(formatAxisValue(105.25)).toBe('105.3');
    expect(formatAxisValue(1.234)).toBe('1.23');
  });

  it('renders an em dash for a non-finite value', () => {
    expect(formatAxisValue(Number.NaN)).toBe('—');
  });
});
