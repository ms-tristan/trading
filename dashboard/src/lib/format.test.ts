import { describe, expect, it } from 'vitest';

import {
  EMPTY_PLACEHOLDER,
  formatDuration,
  formatInteger,
  formatMetric,
  formatMoney,
  formatNumber,
  formatQuantity,
  formatRatioAsPercent,
  formatSignedMoney,
  formatTimestamp,
  humaniseMetricName,
  isFiniteNumber,
  metricFormatFor,
  trendLabel,
  trendOf,
} from './format';

describe('isFiniteNumber', () => {
  it('accepts finite numbers only', () => {
    expect(isFiniteNumber(0)).toBe(true);
    expect(isFiniteNumber(-12.5)).toBe(true);
    expect(isFiniteNumber(Number.NaN)).toBe(false);
    expect(isFiniteNumber(Number.POSITIVE_INFINITY)).toBe(false);
    expect(isFiniteNumber('12')).toBe(false);
    expect(isFiniteNumber(null)).toBe(false);
    expect(isFiniteNumber(undefined)).toBe(false);
  });
});

describe('formatNumber', () => {
  it('renders thousands separators and two decimals by default', () => {
    expect(formatNumber(1234.5)).toBe('1,234.50');
    expect(formatNumber(1234567.891)).toBe('1,234,567.89');
    expect(formatNumber(0)).toBe('0.00');
  });

  it('honours the requested number of decimals', () => {
    expect(formatNumber(1234.567, { decimals: 0 })).toBe('1,235');
    expect(formatNumber(1234.567, { decimals: 4 })).toBe('1,234.5670');
  });

  it('renders an em dash for absent or non-finite values', () => {
    expect(formatNumber(null)).toBe(EMPTY_PLACEHOLDER);
    expect(formatNumber(undefined)).toBe(EMPTY_PLACEHOLDER);
    expect(formatNumber(Number.NaN)).toBe(EMPTY_PLACEHOLDER);
    expect(formatNumber(Number.NEGATIVE_INFINITY)).toBe(EMPTY_PLACEHOLDER);
  });

  it('never renders NaN or undefined', () => {
    const rendered = [
      formatNumber(null),
      formatNumber(Number.NaN),
      formatNumber(undefined),
    ].join(' ');
    expect(rendered).not.toContain('NaN');
    expect(rendered).not.toContain('undefined');
  });
});

describe('formatInteger', () => {
  it('rounds to a grouped integer', () => {
    expect(formatInteger(12345)).toBe('12,345');
    expect(formatInteger(12345.6)).toBe('12,346');
    expect(formatInteger(null)).toBe(EMPTY_PLACEHOLDER);
  });
});

describe('formatMoney', () => {
  it('prefixes the amount with the currency symbol', () => {
    expect(formatMoney(12345.678)).toBe('$12,345.68');
    expect(formatMoney(-1234.5)).toBe('-$1,234.50');
    expect(formatMoney(0)).toBe('$0.00');
  });

  it('accepts another currency symbol and another precision', () => {
    expect(formatMoney(1234.5, { currency: 'EUR ', decimals: 0 })).toBe('EUR 1,235');
  });

  it('renders an em dash for absent values', () => {
    expect(formatMoney(null)).toBe(EMPTY_PLACEHOLDER);
    expect(formatMoney(Number.NaN)).toBe(EMPTY_PLACEHOLDER);
  });
});

describe('formatSignedMoney', () => {
  it('makes the sign explicit for a profit and for a loss', () => {
    expect(formatSignedMoney(12.34)).toBe('+$12.34');
    expect(formatSignedMoney(-12.34)).toBe('-$12.34');
  });

  it('leaves a zero amount unsigned', () => {
    expect(formatSignedMoney(0)).toBe('$0.00');
  });

  it('renders an em dash for absent values', () => {
    expect(formatSignedMoney(undefined)).toBe(EMPTY_PLACEHOLDER);
  });
});

describe('formatRatioAsPercent', () => {
  it('multiplies the fraction by 100', () => {
    expect(formatRatioAsPercent(0.1234)).toBe('12.34%');
    expect(formatRatioAsPercent(1)).toBe('100.00%');
    expect(formatRatioAsPercent(-0.05)).toBe('-5.00%');
  });

  it('adds an explicit sign only when asked', () => {
    expect(formatRatioAsPercent(0.1234, { signed: true })).toBe('+12.34%');
    expect(formatRatioAsPercent(0.1234)).toBe('12.34%');
    expect(formatRatioAsPercent(0, { signed: true })).toBe('0.00%');
  });

  it('renders an em dash for absent values', () => {
    expect(formatRatioAsPercent(null)).toBe(EMPTY_PLACEHOLDER);
  });
});

describe('formatQuantity', () => {
  it('trims trailing zeros', () => {
    expect(formatQuantity(1.5)).toBe('1.5');
    expect(formatQuantity(2)).toBe('2');
    expect(formatQuantity(0.123456789)).toBe('0.123457');
  });

  it('groups thousands and never renders a negative zero', () => {
    expect(formatQuantity(1234.5)).toBe('1,234.5');
    expect(formatQuantity(-0)).toBe('0');
  });

  it('renders an em dash for absent values', () => {
    expect(formatQuantity(null)).toBe(EMPTY_PLACEHOLDER);
  });
});

describe('formatDuration', () => {
  it('renders seconds below one minute', () => {
    expect(formatDuration(0)).toBe('0s');
    expect(formatDuration(45)).toBe('45s');
    expect(formatDuration(59.9)).toBe('59s');
  });

  it('renders a clock above one minute', () => {
    expect(formatDuration(60)).toBe('00:01:00');
    expect(formatDuration(90)).toBe('00:01:30');
    expect(formatDuration(3661)).toBe('01:01:01');
  });

  it('prefixes the number of days', () => {
    expect(formatDuration(90061)).toBe('1d 01:01:01');
    expect(formatDuration(90000)).toBe('1d 01:00:00');
  });

  it('renders an em dash for absent or negative values', () => {
    expect(formatDuration(null)).toBe(EMPTY_PLACEHOLDER);
    expect(formatDuration(-1)).toBe(EMPTY_PLACEHOLDER);
    expect(formatDuration(Number.NaN)).toBe(EMPTY_PLACEHOLDER);
  });
});

describe('formatTimestamp', () => {
  it('renders an ISO-8601 instant in UTC', () => {
    expect(formatTimestamp('2024-01-01T00:00:00+00:00')).toBe('2024-01-01 00:00:00 UTC');
    expect(formatTimestamp('2024-01-01T12:34:56.789Z')).toBe('2024-01-01 12:34:56 UTC');
    expect(formatTimestamp(new Date(Date.UTC(2024, 11, 31, 23, 59, 59)))).toBe(
      '2024-12-31 23:59:59 UTC',
    );
  });

  it('converts an offset timestamp to UTC', () => {
    expect(formatTimestamp('2024-01-01T02:00:00+02:00')).toBe('2024-01-01 00:00:00 UTC');
  });

  it('renders an em dash for absent or unparsable values', () => {
    expect(formatTimestamp(null)).toBe(EMPTY_PLACEHOLDER);
    expect(formatTimestamp(undefined)).toBe(EMPTY_PLACEHOLDER);
    expect(formatTimestamp('')).toBe(EMPTY_PLACEHOLDER);
    expect(formatTimestamp('not-a-date')).toBe(EMPTY_PLACEHOLDER);
  });
});

describe('trendOf and trendLabel', () => {
  it('classifies a value and labels it in words', () => {
    expect(trendOf(1)).toBe('up');
    expect(trendLabel(1)).toBe('Up');
    expect(trendOf(-1)).toBe('down');
    expect(trendLabel(-1)).toBe('Down');
    expect(trendOf(0)).toBe('flat');
    expect(trendLabel(0)).toBe('Flat');
  });

  it('treats absent values as flat', () => {
    expect(trendOf(null)).toBe('flat');
    expect(trendLabel(null)).toBe('Flat');
    expect(trendLabel(Number.NaN)).toBe('Flat');
  });
});

describe('humaniseMetricName', () => {
  it('turns a snake_case metric name into a readable label', () => {
    expect(humaniseMetricName('max_drawdown')).toBe('Max drawdown');
    expect(humaniseMetricName('sharpe_ratio')).toBe('Sharpe ratio');
    expect(humaniseMetricName('cagr')).toBe('CAGR');
  });

  it('falls back to a generic transformation for an unknown name', () => {
    expect(humaniseMetricName('my_custom_metric')).toBe('My custom metric');
  });

  it('renders an em dash for an empty name', () => {
    expect(humaniseMetricName('')).toBe(EMPTY_PLACEHOLDER);
  });
});

describe('metricFormatFor', () => {
  it('routes every documented metric family', () => {
    expect(metricFormatFor('total_return')).toBe('percent');
    expect(metricFormatFor('win_rate')).toBe('percent');
    expect(metricFormatFor('max_drawdown')).toBe('percent');
    expect(metricFormatFor('avg_trade_pnl')).toBe('money');
    expect(metricFormatFor('final_balance')).toBe('money');
    expect(metricFormatFor('n_trades')).toBe('count');
    expect(metricFormatFor('max_drawdown_duration')).toBe('duration');
    expect(metricFormatFor('sharpe_ratio')).toBe('ratio');
    expect(metricFormatFor('profit_factor')).toBe('ratio');
  });

  it('falls back to a neutral ratio for an unknown metric', () => {
    expect(metricFormatFor('mystery_metric')).toBe('ratio');
  });
});

describe('formatMetric', () => {
  it('delegates to the formatter of the metric family', () => {
    expect(formatMetric('total_return', 0.5)).toBe('50.00%');
    expect(formatMetric('avg_win', 12.5)).toBe('$12.50');
    expect(formatMetric('n_trades', 12)).toBe('12');
    expect(formatMetric('sharpe_ratio', 1.5)).toBe('1.50');
    expect(formatMetric('max_drawdown_duration', 90)).toBe('00:01:30');
  });

  it('renders an em dash for an absent value, whatever the family', () => {
    expect(formatMetric('total_return', null)).toBe(EMPTY_PLACEHOLDER);
    expect(formatMetric('n_trades', null)).toBe(EMPTY_PLACEHOLDER);
    expect(formatMetric('avg_win', Number.NaN)).toBe(EMPTY_PLACEHOLDER);
  });
});

describe('locale determinism', () => {
  it('always formats with the en-US convention (no hydration mismatch)', () => {
    expect(formatNumber(1234567.5)).toBe('1,234,567.50');
    expect(formatMoney(1234567.5)).toBe('$1,234,567.50');
    expect(formatQuantity(1234567.5)).toBe('1,234,567.5');
  });
});
