import { describe, expect, it } from 'vitest';

import {
  CANDLE_RENDER_LIMIT,
  candleDirection,
  candleTime,
  positionPriceLines,
  tradeMarkers,
  visibleCandles,
} from './candles';
import type { Candle, Position, TradeRecord } from './types';

function candle(overrides: Partial<Candle> = {}): Candle {
  return {
    profile_id: 'btc-paper',
    timestamp: '2024-01-01T00:00:00+00:00',
    open: 100,
    high: 110,
    low: 90,
    close: 105,
    volume: 12.5,
    closed: true,
    ...overrides,
  };
}

function trade(overrides: Partial<TradeRecord> = {}): TradeRecord {
  return {
    entry_time: '2024-01-01T00:00:00+00:00',
    exit_time: '2024-01-01T01:00:00+00:00',
    entry_price: 20000,
    exit_price: 20500,
    size: 0.5,
    direction: 'long',
    pnl: 250,
    pnl_pct: 0.0125,
    fees: 4.5,
    exit_reason: 'take_profit',
    duration_minutes: 60,
    stop_price: 19500,
    take_profit_price: 21000,
    params_id: 'default',
    ...overrides,
  };
}

function position(overrides: Partial<Position> = {}): Position {
  return {
    profile_id: 'btc-paper',
    symbol: 'BTC/USDT',
    quantity: 1.5,
    average_price: 20000,
    direction: 'long',
    opened_at: '2024-01-01T00:00:00+00:00',
    updated_at: '2024-01-01T01:00:00+00:00',
    realized_pnl: 12.5,
    unrealized_pnl: -25.75,
    stop_price: 19000,
    ...overrides,
  };
}

describe('visibleCandles', () => {
  it('returns an empty array for an empty input', () => {
    expect(visibleCandles([])).toEqual([]);
  });

  it('returns every candle when there are fewer than the limit', () => {
    const candles = [candle({ timestamp: '2024-01-01T00:00:00+00:00' }), candle({ timestamp: '2024-01-01T01:00:00+00:00' })];

    const visible = visibleCandles(candles, 10);

    expect(visible).toEqual(candles);
    expect(visible).not.toBe(candles);
  });

  it('returns exactly the limit when the input is exactly that long', () => {
    const candles = Array.from({ length: 10 }, (_, index) =>
      candle({ timestamp: `2024-01-01T0${index}:00:00+00:00` }),
    );

    expect(visibleCandles(candles, 10)).toHaveLength(10);
  });

  it('keeps the newest candles, in input order, when there are too many', () => {
    const candles = Array.from({ length: 12 }, (_, index) =>
      candle({ timestamp: `2024-01-01T${String(index).padStart(2, '0')}:00:00+00:00`, close: index }),
    );

    const visible = visibleCandles(candles, 5);

    expect(visible).toHaveLength(5);
    expect(visible.map((entry) => entry.close)).toEqual([7, 8, 9, 10, 11]);
  });

  it('caps the window at the render limit by default', () => {
    const candles = Array.from({ length: CANDLE_RENDER_LIMIT + 25 }, (_, index) =>
      candle({ timestamp: `2024-01-01T00:00:${String(index % 60).padStart(2, '0')}+00:00` }),
    );

    expect(visibleCandles(candles)).toHaveLength(CANDLE_RENDER_LIMIT);
  });

  it('falls back to the render limit for an unusable limit instead of throwing', () => {
    const candles = [candle(), candle({ timestamp: '2024-01-01T01:00:00+00:00' })];

    expect(visibleCandles(candles, 0)).toHaveLength(2);
    expect(visibleCandles(candles, -5)).toHaveLength(2);
    expect(visibleCandles(candles, Number.NaN)).toHaveLength(2);
  });
});

describe('tradeMarkers', () => {
  it('returns an empty array for an empty input', () => {
    expect(tradeMarkers([])).toEqual([]);
  });

  it('marks the entry and the exit of a long trade', () => {
    const markers = tradeMarkers([trade()]);

    expect(markers).toHaveLength(2);
    expect(markers[0]).toEqual({
      time: 1704067200,
      position: 'belowBar',
      color: 'var(--color-chart-marker-entry)',
      shape: 'arrowUp',
      text: 'Long 20,000.00',
    });
    expect(markers[1]).toEqual({
      time: 1704070800,
      position: 'aboveBar',
      color: 'var(--color-chart-marker-exit)',
      shape: 'arrowDown',
      text: 'take_profit 20,500.00',
    });
  });

  it('mirrors the markers of a short trade', () => {
    const markers = tradeMarkers([trade({ direction: 'short', exit_reason: 'stop_loss' })]);

    expect(markers[0]?.position).toBe('aboveBar');
    expect(markers[0]?.shape).toBe('arrowDown');
    expect(markers[0]?.text).toBe('Short 20,000.00');
    expect(markers[1]?.position).toBe('belowBar');
    expect(markers[1]?.shape).toBe('arrowUp');
    expect(markers[1]?.text).toBe('stop_loss 20,500.00');
  });

  it('emits two markers per trade, in input order', () => {
    const markers = tradeMarkers([trade(), trade({ direction: 'short' })]);

    expect(markers).toHaveLength(4);
    expect(markers.map((marker) => marker.shape)).toEqual([
      'arrowUp',
      'arrowDown',
      'arrowDown',
      'arrowUp',
    ]);
  });

  it('skips a record whose time or price is unusable', () => {
    const markers = tradeMarkers([
      trade({ exit_time: 'not a date' }),
      trade({ entry_price: Number.NaN }),
      trade({ exit_price: Number.POSITIVE_INFINITY }),
      trade({ entry_time: '' }),
      trade({ entry_time: '2024-01-02T00:00:00+00:00' }),
    ]);

    expect(markers).toHaveLength(2);
    expect(markers[0]?.time).toBe(1704153600);
  });
});

describe('positionPriceLines', () => {
  it('returns an empty array for an empty list', () => {
    expect(positionPriceLines([])).toEqual([]);
  });

  it('draws the average line and the stop line of an open position', () => {
    expect(positionPriceLines([position()])).toEqual([
      {
        price: 20000,
        color: 'var(--color-chart-average)',
        title: 'Average',
        lineStyle: 'solid',
      },
      { price: 19000, color: 'var(--color-chart-stop)', title: 'Stop', lineStyle: 'dashed' },
    ]);
  });

  it('draws only the average line when no stop is set', () => {
    const lines = positionPriceLines([position({ stop_price: null })]);

    expect(lines).toHaveLength(1);
    expect(lines[0]?.title).toBe('Average');
  });

  it('draws nothing when neither the average price nor the stop is set', () => {
    expect(positionPriceLines([position({ average_price: null, stop_price: null })])).toEqual([]);
  });

  it('ignores a non-finite average price or stop', () => {
    const lines = positionPriceLines([
      position({ average_price: Number.NaN, stop_price: 19000 }),
      position({ average_price: 21000, stop_price: Number.POSITIVE_INFINITY }),
    ]);

    expect(lines).toEqual([
      { price: 19000, color: 'var(--color-chart-stop)', title: 'Stop', lineStyle: 'dashed' },
      {
        price: 21000,
        color: 'var(--color-chart-average)',
        title: 'Average',
        lineStyle: 'solid',
      },
    ]);
  });
});

describe('candleTime', () => {
  it('converts an ISO-8601 timestamp to UNIX seconds', () => {
    expect(candleTime('2024-01-01T00:00:00+00:00')).toBe(1704067200);
    expect(candleTime('2024-01-01T00:00:00.750Z')).toBe(1704067200);
    expect(candleTime('1970-01-01T00:00:01Z')).toBe(1);
  });

  it('returns null for an unparsable timestamp', () => {
    expect(candleTime('not a date')).toBeNull();
    expect(candleTime('2024-13-45T99:99:99Z')).toBeNull();
  });

  it('returns null for an empty or absent timestamp', () => {
    expect(candleTime('')).toBeNull();
    expect(candleTime('   ')).toBeNull();
    expect(candleTime(undefined as unknown as string)).toBeNull();
  });
});

describe('candleDirection', () => {
  it('reports a bullish candle when the close is above the open', () => {
    expect(candleDirection(candle({ open: 100, close: 105 }))).toBe('bull');
  });

  it('reports a bearish candle when the close is below the open', () => {
    expect(candleDirection(candle({ open: 105, close: 100 }))).toBe('bear');
  });

  it('reports a flat candle when the bounds are equal', () => {
    expect(candleDirection(candle({ open: 100, close: 100 }))).toBe('flat');
  });

  it('reports a flat candle when a bound is missing', () => {
    expect(candleDirection(candle({ open: null }))).toBe('flat');
    expect(candleDirection(candle({ close: null }))).toBe('flat');
    expect(candleDirection(candle({ close: Number.NaN }))).toBe('flat');
  });
});
