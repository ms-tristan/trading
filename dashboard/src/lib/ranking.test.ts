import { describe, expect, it } from 'vitest';

import type { ProfileSnapshot } from '@/lib/types';

import { portfolioValueOf, sortProfilesByPortfolioValue } from './ranking';

/**
 * A profile reduced to the two fields the ranking reads.
 *
 * `equity` is the attributed equity of the profile (`allocation + realized_pnl +
 * unrealized_pnl`); every other field of the frozen contract is irrelevant here
 * and is filled with a plausible value so the fixture stays a valid
 * `ProfileSnapshot`.
 */
function profile(profileId: string, equity: number | null): ProfileSnapshot {
  return {
    profile_id: profileId,
    symbol: 'BTC/USDT',
    timeframe: '1h',
    strategy: 'BasicStrategy',
    mode: 'paper',
    status: 'running',
    initial_balance: 10000,
    equity,
    cash: 10000,
    position_value: 0,
    total_return: 0,
    n_trades: 0,
    open_positions: 0,
    health: {
      profile_id: profileId,
      status: 'running',
      last_candle_at: null,
      lag_seconds: null,
      last_error: null,
      reconnect_count: 0,
      counters: {
        candles_processed: 0,
        orders_submitted: 0,
        orders_filled: 0,
        orders_rejected: 0,
        stream_reconnects: 0,
        risk_rejections: 0,
        errors: 0,
      },
    },
    started_at: null,
    updated_at: null,
  };
}

/** The ids of a profile list, in order: the readable form of a ranking. */
function ids(profiles: ProfileSnapshot[]): string[] {
  return profiles.map((entry) => entry.profile_id);
}

describe('portfolioValueOf', () => {
  it('is the attributed equity of the profile', () => {
    expect(portfolioValueOf(profile('alpha', 10450.5))).toBe(10450.5);
    expect(portfolioValueOf(profile('alpha', 0))).toBe(0);
    expect(portfolioValueOf(profile('alpha', -120.25))).toBe(-120.25);
  });

  it('ranks an absent or non-finite equity last, never as a zero', () => {
    expect(portfolioValueOf(profile('alpha', null))).toBe(Number.NEGATIVE_INFINITY);
    expect(portfolioValueOf(profile('alpha', Number.NaN))).toBe(Number.NEGATIVE_INFINITY);
    expect(portfolioValueOf(profile('alpha', Number.POSITIVE_INFINITY))).toBe(
      Number.NEGATIVE_INFINITY,
    );
    // A real zero is a real measure: it is *not* the unknown-value marker.
    expect(portfolioValueOf(profile('alpha', 0))).toBeGreaterThan(
      portfolioValueOf(profile('beta', null)),
    );
  });
});

describe('sortProfilesByPortfolioValue', () => {
  it('orders the profiles by equity, highest first', () => {
    const sorted = sortProfilesByPortfolioValue([
      profile('beta', 9900),
      profile('alpha', 10450.5),
      profile('gamma', 12000),
    ]);

    expect(ids(sorted)).toEqual(['gamma', 'alpha', 'beta']);
  });

  it('ranks a profile without a finite equity last', () => {
    const sorted = sortProfilesByPortfolioValue([
      profile('unknown', null),
      profile('low', 10),
      profile('high', 20000),
    ]);

    expect(ids(sorted)).toEqual(['high', 'low', 'unknown']);
  });

  it('breaks a tie by profile_id ascending', () => {
    const sorted = sortProfilesByPortfolioValue([
      profile('charlie', 5000),
      profile('alpha', 5000),
      profile('bravo', 5000),
    ]);

    expect(ids(sorted)).toEqual(['alpha', 'bravo', 'charlie']);
  });

  it('applies the same id tie-break to two profiles of unknown equity', () => {
    const sorted = sortProfilesByPortfolioValue([
      profile('zulu', null),
      profile('alpha', null),
    ]);

    expect(ids(sorted)).toEqual(['alpha', 'zulu']);
  });

  it('never mutates the input and always returns a fresh array', () => {
    const input = [profile('beta', 10), profile('alpha', 20)];
    const snapshot = [...input];

    const sorted = sortProfilesByPortfolioValue(input);

    expect(sorted).not.toBe(input);
    expect(input).toEqual(snapshot);
    expect(ids(input)).toEqual(['beta', 'alpha']);
  });

  it('is idempotent and independent of the input order', () => {
    const profiles = [
      profile('delta', 400),
      profile('alpha', 100),
      profile('charlie', 300),
      profile('bravo', 200),
    ];
    const shuffled = [profiles[2], profiles[0], profiles[3], profiles[1]];

    const once = sortProfilesByPortfolioValue(profiles);
    const twice = sortProfilesByPortfolioValue(once);
    const fromShuffled = sortProfilesByPortfolioValue(shuffled);

    expect(ids(once)).toEqual(['delta', 'charlie', 'bravo', 'alpha']);
    expect(ids(twice)).toEqual(ids(once));
    expect(ids(fromShuffled)).toEqual(ids(once));
  });

  it('keeps the order total for equal equity and equal id', () => {
    const sorted = sortProfilesByPortfolioValue([
      profile('same', 1234),
      profile('same', 1234),
      profile('other', 1234),
    ]);

    // Every measure is equal, so the id decides: 'other' precedes both 'same'
    // entries, and the two indistinguishable ones stay adjacent without the
    // comparator throwing — it answers `0` for that pair.
    expect(ids(sorted)).toEqual(['other', 'same', 'same']);
  });

  it('handles the empty list', () => {
    expect(sortProfilesByPortfolioValue([])).toEqual([]);
  });
});
