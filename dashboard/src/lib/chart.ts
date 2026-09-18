/**
 * Pure geometry of the hand-rolled equity chart.
 *
 * The dashboard deliberately ships no charting dependency: the curve is inline
 * SVG built from these helpers, which keeps the rendering deterministic (and
 * testable in jsdom) and the client bundle small. Nothing here touches React,
 * the DOM or the network.
 *
 * Two rules drive every function:
 *
 * * a scale is **never** degenerate — a flat or single-value series produces a
 *   positive-height domain, so no caller can divide by zero;
 * * a missing value is a **gap**, never a zero: a series path starts a new
 *   subpath after a `null` point instead of dropping to the axis.
 */

import { EMPTY_PLACEHOLDER, formatNumber, isFiniteNumber } from './format';
import type { EquityPoint } from './types';

/** The three series the equity chart draws, in legend order. */
export const SERIES_KEYS = ['equity', 'cash', 'position_value'] as const;

/** Key of one drawable series of {@link EquityPoint}. */
export type SeriesKey = (typeof SERIES_KEYS)[number];

/** Vertical domain and tick values of a chart. */
export interface ChartScale {
  /** Lowest value of the axis (the domain is closed: `[min, max]`). */
  min: number;
  /** Highest value of the axis; always strictly greater than {@link min}. */
  max: number;
  /** Ascending, evenly spaced tick values covering the domain. */
  ticks: number[];
}

/** One drawable series: an SVG path plus the geometry behind it. */
export interface SeriesPath {
  /** SVG `d` attribute; empty when the series holds no usable value. */
  path: string;
  /**
   * One entry per input point, in input order, so an x-index stays aligned with
   * the payload. A point with a missing value carries `y: Number.NaN`: it is a
   * gap in {@link path}, not a coordinate.
   */
  coords: { x: number; y: number }[];
}

/** Tick values of the default axis. */
const DEFAULT_TICK_COUNT = 4;

/** Multipliers that turn a raw step into a readable one (1, 2, 2.5, 5, 10). */
const NICE_STEPS = [1, 2, 2.5, 5, 10] as const;

/** Relative padding applied around a flat series (5 % of its value). */
const FLAT_PADDING_RATIO = 0.05;

/** Absolute padding applied around a flat series, so a zero value still scales. */
const MIN_FLAT_PADDING = 1;

/** Keep geometry free of binary floating-point noise (`0.1 + 0.2`). */
function round(value: number): number {
  return Number(value.toPrecision(12));
}

/** Round a coordinate to a compact, stable string form. */
function coordinate(value: number): string {
  return String(Math.round(value * 1000) / 1000);
}

/**
 * Every finite value the y-domain of an {@link EquityPoint} series set is built
 * from: the three series share one axis, otherwise the curves would not be
 * comparable.
 */
export function chartValues(points: EquityPoint[]): number[] {
  const values: number[] = [];
  for (const point of points) {
    for (const key of SERIES_KEYS) {
      const value = point[key];
      if (isFiniteNumber(value)) {
        values.push(value);
      }
    }
  }
  return values;
}

/** Round `rawStep` up to the next readable step (1, 2, 2.5, 5 or 10 x 10^n). */
function niceStep(rawStep: number): number {
  if (!Number.isFinite(rawStep) || rawStep <= 0) {
    return 1;
  }
  const magnitude = Math.pow(10, Math.floor(Math.log10(rawStep)));
  const normalized = rawStep / magnitude;
  const multiplier = NICE_STEPS.find((step) => normalized <= step) ?? 10;
  return multiplier * magnitude;
}

/**
 * Build a readable, non-degenerate vertical scale for `values`.
 *
 * * an empty (or all-`null`) input yields the neutral `[0, 1]` domain;
 * * a flat or single-value input is padded, so `min < max` always holds;
 * * {@link ChartScale.ticks} always starts at `min`, ends at `max` and is evenly
 *   spaced, which is what the axis row and the grid lines are drawn from.
 */
export function niceScale(
  values: (number | null)[],
  tickCount: number = DEFAULT_TICK_COUNT,
): ChartScale {
  const count = Number.isInteger(tickCount) && tickCount >= 2 ? tickCount : DEFAULT_TICK_COUNT;

  let min = Number.POSITIVE_INFINITY;
  let max = Number.NEGATIVE_INFINITY;
  for (const value of values) {
    if (!isFiniteNumber(value)) {
      continue;
    }
    min = Math.min(min, value);
    max = Math.max(max, value);
  }

  if (min === Number.POSITIVE_INFINITY || max === Number.NEGATIVE_INFINITY) {
    return { min: 0, max: 1, ticks: [0, 1] };
  }

  if (min === max) {
    const padding = Math.max(Math.abs(min) * FLAT_PADDING_RATIO, MIN_FLAT_PADDING);
    min -= padding;
    max += padding;
  }

  const step = niceStep((max - min) / (count - 1));
  const start = round(Math.floor(min / step) * step);
  const end = round(Math.ceil(max / step) * step);
  const segments = Math.max(1, Math.round(round((end - start) / step)));
  const ticks = Array.from({ length: segments + 1 }, (_, index) => round(start + index * step));

  const first = ticks[0] ?? 0;
  const last = ticks[ticks.length - 1] ?? first + step;
  return { min: first, max: last > first ? last : first + step, ticks };
}

/**
 * Project one series of `points` into the box `width` x `height`.
 *
 * The y-domain is shared by the three series (see {@link chartValues}), so a
 * caller scaling the axis with `niceScale(chartValues(points))` gets exactly the
 * domain used here.
 *
 * Horizontal positions are evenly spaced; a single point is centred. Missing
 * values break the path (a new `M` command) and are reported as `NaN`
 * coordinates, so the curve never drops to zero and never bridges a gap.
 */
export function buildSeries(
  points: EquityPoint[],
  key: SeriesKey,
  width: number,
  height: number,
  padding: number,
): SeriesPath {
  const safeWidth = Number.isFinite(width) && width > 0 ? width : 1;
  const safeHeight = Number.isFinite(height) && height > 0 ? height : 1;
  const safePadding = Number.isFinite(padding) && padding > 0 ? padding : 0;
  const innerWidth = Math.max(safeWidth - safePadding * 2, 1);
  const innerHeight = Math.max(safeHeight - safePadding * 2, 1);

  const scale = niceScale(chartValues(points));
  const span = scale.max - scale.min;
  const coords: { x: number; y: number }[] = [];
  const commands: string[] = [];
  let penDown = false;

  points.forEach((point, index) => {
    const x =
      points.length <= 1
        ? safePadding + innerWidth / 2
        : safePadding + (index / (points.length - 1)) * innerWidth;

    const value = point[key];
    if (!isFiniteNumber(value) || span <= 0) {
      coords.push({ x: round(x), y: Number.NaN });
      penDown = false;
      return;
    }

    const ratio = (value - scale.min) / span;
    const y = safePadding + (1 - ratio) * innerHeight;
    coords.push({ x: round(x), y: round(y) });
    commands.push(`${penDown ? 'L' : 'M'}${coordinate(x)} ${coordinate(y)}`);
    penDown = true;
  });

  return { path: commands.join(' '), coords };
}

/**
 * Index of the coordinate whose `x` is closest to `x`.
 *
 * Returns `-1` for an empty coordinate list, and the first index for a
 * non-finite cursor. Ties resolve to the lower index.
 */
export function nearestIndex(coords: { x: number; y: number }[], x: number): number {
  if (coords.length === 0) {
    return -1;
  }
  let best = 0;
  let bestDistance = Number.POSITIVE_INFINITY;
  coords.forEach((coord, index) => {
    const distance = Math.abs(coord.x - x);
    if (distance < bestDistance) {
      bestDistance = distance;
      best = index;
    }
  });
  return best;
}

/**
 * Render one y-axis tick: grouped, with a precision that fits the magnitude
 * (`10500` -> `10,500`, `105.25` -> `105.25`).
 */
export function formatAxisValue(value: number): string {
  if (!isFiniteNumber(value)) {
    return EMPTY_PLACEHOLDER;
  }
  const magnitude = Math.abs(value);
  const decimals = magnitude >= 1000 ? 0 : magnitude >= 10 ? 1 : 2;
  return formatNumber(value, { decimals });
}
