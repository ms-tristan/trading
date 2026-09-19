import { afterEach, describe, expect, it, vi } from 'vitest';

import { CANDLE_TOKEN_NAMES, chartTheme, chartToken } from './chart-theme';

/** Stub `getComputedStyle` with a plain token map. */
function stubComputedStyle(values: Record<string, string>): void {
  vi.stubGlobal(
    'getComputedStyle',
    vi.fn(() => ({
      getPropertyValue: (name: string) => values[name] ?? '',
    })),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('CANDLE_TOKEN_NAMES', () => {
  it('lists the ten chart tokens plus the shared grid and axis tokens', () => {
    expect([...CANDLE_TOKEN_NAMES]).toEqual([
      '--color-chart-bull',
      '--color-chart-bear',
      '--color-chart-bull-hollow',
      '--color-chart-wick-bull',
      '--color-chart-wick-bear',
      '--color-chart-crosshair',
      '--color-chart-marker-entry',
      '--color-chart-marker-exit',
      '--color-chart-stop',
      '--color-chart-average',
      '--color-chart-grid',
      '--color-chart-axis',
    ]);
  });
});

describe('chartToken', () => {
  it('returns the trimmed computed value of a token', () => {
    stubComputedStyle({ '--color-chart-bull': '  #22c55e  ' });

    expect(chartToken('--color-chart-bull')).toBe('#22c55e');
  });

  it('returns undefined for a token that resolves to an empty value', () => {
    stubComputedStyle({ '--color-chart-bull': '   ' });

    expect(chartToken('--color-chart-bull')).toBeUndefined();
  });

  it('returns undefined for an unknown token', () => {
    stubComputedStyle({});

    expect(chartToken('--color-chart-unknown')).toBeUndefined();
  });

  it('never throws when there is no document', () => {
    stubComputedStyle({ '--color-chart-bull': '#22c55e' });
    vi.stubGlobal('document', undefined);

    expect(() => chartToken('--color-chart-bull')).not.toThrow();
    expect(chartToken('--color-chart-bull')).toBeUndefined();
  });

  it('never throws when getComputedStyle throws', () => {
    vi.stubGlobal(
      'getComputedStyle',
      vi.fn(() => {
        throw new Error('no style engine');
      }),
    );

    expect(() => chartToken('--color-chart-bull')).not.toThrow();
    expect(chartToken('--color-chart-bull')).toBeUndefined();
  });

  it('never throws when getComputedStyle is unavailable', () => {
    vi.stubGlobal('getComputedStyle', undefined);

    expect(chartToken('--color-chart-bull')).toBeUndefined();
  });
});

describe('chartTheme', () => {
  it('exposes every token name, resolved', () => {
    const values: Record<string, string> = {};
    for (const name of CANDLE_TOKEN_NAMES) {
      values[name] = `value-of-${name}`;
    }
    stubComputedStyle(values);

    const theme = chartTheme();

    expect(Object.keys(theme)).toEqual([...CANDLE_TOKEN_NAMES]);
    for (const name of CANDLE_TOKEN_NAMES) {
      expect(theme[name]).toBe(`value-of-${name}`);
    }
  });

  it('keeps an explicit undefined for a token the document does not define', () => {
    stubComputedStyle({});

    const theme = chartTheme();

    expect(Object.keys(theme)).toHaveLength(CANDLE_TOKEN_NAMES.length);
    expect(theme['--color-chart-bull']).toBeUndefined();
  });
});
