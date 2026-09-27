import { describe, expect, it } from 'vitest';

import type { ProfileSnapshot, ProfilesPayload, RunMode, WalletSnapshot } from '@/lib/types';

import { accountPerformance, ledgerPerformance, resolveModeWallet } from './performance';

/** A wallet snapshot with every money field the hero reads. */
function wallet(overrides: Partial<WalletSnapshot> = {}): WalletSnapshot {
  return {
    name: 'usdt',
    mode: 'paper',
    initial_balance: 25000,
    cash: 21500,
    equity: 25750,
    deployed: 4500,
    realized_pnl: 1000,
    unrealized_pnl: -250,
    total_exposure: 4700,
    profiles: 2,
    source: 'local',
    updated_at: '2024-01-01T00:00:00+00:00',
    total_cash: 21500,
    positions_value: 4250,
    total_portfolio_value: 25750,
    ...overrides,
  };
}

/** One profile of the payload, reduced to the fields the summary reads. */
function profile(
  profileId: string,
  overrides: Partial<ProfileSnapshot> & { mode?: RunMode } = {},
): ProfileSnapshot {
  return {
    profile_id: profileId,
    symbol: 'BTC/USDT',
    timeframe: '1h',
    strategy: 'BasicStrategy',
    mode: 'paper',
    status: 'running',
    initial_balance: 10000,
    equity: 10450.5,
    cash: 8000,
    position_value: 2450.5,
    total_return: 0.045,
    n_trades: 12,
    open_positions: 1,
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
    ...overrides,
  };
}

/** The real ledger of the fixtures: clearly different figures from the paper one. */
const liveWallet = wallet({
  name: 'binance',
  mode: 'live',
  initial_balance: 5000,
  cash: 4900,
  equity: 5120,
  deployed: 220,
  realized_pnl: 20,
  unrealized_pnl: 100,
  total_exposure: 220,
  profiles: 1,
  source: 'venue',
  total_cash: 4900,
  positions_value: 300,
  total_portfolio_value: 5200,
});

describe('ledgerPerformance', () => {
  it('derives the value, the USDT return and the ratio from the ledger', () => {
    const performance = ledgerPerformance(wallet());

    expect(performance.portfolioValue).toBe(25750);
    expect(performance.totalReturn).toBe(750);
    expect(performance.totalReturnRatio).toBe(750 / 25000);
  });

  it('renders the value but no return when the initial balance is missing', () => {
    for (const initialBalance of [null, Number.NaN]) {
      const performance = ledgerPerformance(wallet({ initial_balance: initialBalance }));

      expect(performance.portfolioValue).toBe(25750);
      expect(performance.totalReturn).toBeNull();
      expect(performance.totalReturnRatio).toBeNull();
    }
  });

  it('never invents a percentage from a zero initial balance', () => {
    const performance = ledgerPerformance(wallet({ initial_balance: 0 }));

    expect(performance.portfolioValue).toBe(25750);
    expect(performance.totalReturn).toBeNull();
    expect(performance.totalReturnRatio).toBeNull();
  });

  it('answers nulls for a wallet that is absent or explicitly null', () => {
    for (const absent of [null, undefined]) {
      expect(ledgerPerformance(absent)).toEqual({
        portfolioValue: null,
        totalReturn: null,
        totalReturnRatio: null,
      });
    }
  });

  it('falls back to the sum of the two totals when the server emits no total', () => {
    const performance = ledgerPerformance(
      wallet({ total_portfolio_value: undefined, total_cash: 21500, positions_value: 4250 }),
    );

    expect(performance.portfolioValue).toBe(25750);
    expect(performance.totalReturn).toBe(750);
  });

  it('falls back to the ledger equity when no total is available at all', () => {
    const performance = ledgerPerformance(
      wallet({
        total_portfolio_value: undefined,
        total_cash: undefined,
        positions_value: undefined,
        equity: 26200,
      }),
    );

    expect(performance.portfolioValue).toBe(26200);
    expect(performance.totalReturn).toBe(1200);
  });

  it('ignores a non-finite total and reads the parts instead', () => {
    const performance = ledgerPerformance(
      wallet({ total_portfolio_value: Number.NaN, total_cash: 100, positions_value: 50 }),
    );

    expect(performance.portfolioValue).toBe(150);
    expect(performance.totalReturn).toBe(-24850);
  });

  it('answers nulls when the ledger carries no usable value at all', () => {
    const performance = ledgerPerformance(
      wallet({
        total_portfolio_value: undefined,
        total_cash: null,
        positions_value: null,
        equity: null,
      }),
    );

    expect(performance).toEqual({
      portfolioValue: null,
      totalReturn: null,
      totalReturnRatio: null,
    });
  });

  it('reads a real zero value as zero, with a -100% return', () => {
    const performance = ledgerPerformance(
      wallet({ total_portfolio_value: 0, initial_balance: 1000 }),
    );

    expect(performance.portfolioValue).toBe(0);
    expect(performance.totalReturn).toBe(-1000);
    expect(performance.totalReturnRatio).toBe(-1);
  });

  it('reads an unchanged ledger as a flat return', () => {
    const performance = ledgerPerformance(
      wallet({ total_portfolio_value: 25000, initial_balance: 25000 }),
    );

    expect(performance.totalReturn).toBe(0);
    expect(performance.totalReturnRatio).toBe(0);
  });
});

describe('resolveModeWallet', () => {
  it('reads the entry of the requested mode', () => {
    const payload: ProfilesPayload = {
      profiles: [],
      generated_at: null,
      wallet: wallet(),
      wallets: { paper: wallet(), live: liveWallet },
    };

    expect(resolveModeWallet(payload, 'paper')?.name).toBe('usdt');
    expect(resolveModeWallet(payload, 'live')?.name).toBe('binance');
  });

  it('never hands the paper ledger to the real mode', () => {
    const payload: ProfilesPayload = {
      profiles: [],
      generated_at: null,
      wallet: wallet(),
      wallets: { paper: null, live: null },
    };

    expect(resolveModeWallet(payload, 'paper')).toBeNull();
    expect(resolveModeWallet(payload, 'live')).toBeNull();
  });

  it('falls back to `wallet` for paper only, on a server without `wallets`', () => {
    const payload: ProfilesPayload = {
      profiles: [],
      generated_at: null,
      wallet: wallet(),
    };

    expect(resolveModeWallet(payload, 'paper')?.name).toBe('usdt');
    expect(resolveModeWallet(payload, 'live')).toBeNull();
  });
});

describe('accountPerformance', () => {
  const payload = (overrides: Partial<ProfilesPayload> = {}): ProfilesPayload => ({
    profiles: [
      profile('alpha', { total_return: 0.045 }),
      profile('bravo', { total_return: -0.02 }),
      profile('live-1', { mode: 'live', total_return: 0.01 }),
    ],
    generated_at: '2024-01-01T00:00:00+00:00',
    wallet: wallet(),
    wallets: { paper: wallet(), live: liveWallet },
    ...overrides,
  });

  it('reads the ledger, the P&L and the deployed capital of the mode', () => {
    const summary = accountPerformance(payload(), 'paper');

    expect(summary.ledger.portfolioValue).toBe(25750);
    expect(summary.ledger.totalReturnRatio).toBe(750 / 25000);
    expect(summary.realizedPnl).toBe(1000);
    expect(summary.unrealizedPnl).toBe(-250);
    expect(summary.deployed).toBe(4500);
  });

  it('counts the profiles that run out of the profiles configured, per mode', () => {
    const mixed = payload({
      profiles: [
        profile('alpha'),
        profile('bravo', { status: 'halted' }),
        profile('charlie', { status: 'starting' }),
        profile('live-1', { mode: 'live', status: 'running' }),
        profile('live-2', { mode: 'live', status: 'stopped' }),
      ],
    });

    const paper = accountPerformance(mixed, 'paper');
    expect(paper.profilesRunning).toBe(1);
    expect(paper.profilesConfigured).toBe(3);

    const live = accountPerformance(mixed, 'live');
    expect(live.profilesRunning).toBe(1);
    expect(live.profilesConfigured).toBe(2);
  });

  it('picks the best and the worst profile by total return', () => {
    const summary = accountPerformance(
      payload({
        profiles: [
          profile('alpha', { total_return: 0.045 }),
          profile('bravo', { total_return: -0.02 }),
          profile('charlie', { total_return: 0.12 }),
        ],
      }),
      'paper',
    );

    expect(summary.best).toEqual({ profileId: 'charlie', totalReturn: 0.12 });
    expect(summary.worst).toEqual({ profileId: 'bravo', totalReturn: -0.02 });
  });

  it('breaks a tie by profile_id ascending, for both extremes', () => {
    const summary = accountPerformance(
      payload({
        profiles: [
          profile('zulu', { total_return: 0.05 }),
          profile('alpha', { total_return: 0.05 }),
          profile('mike', { total_return: -0.05 }),
          profile('bravo', { total_return: -0.05 }),
        ],
      }),
      'paper',
    );

    expect(summary.best).toEqual({ profileId: 'alpha', totalReturn: 0.05 });
    expect(summary.worst).toEqual({ profileId: 'bravo', totalReturn: -0.05 });
  });

  it('excludes the profiles without a finite total return', () => {
    const summary = accountPerformance(
      payload({
        profiles: [
          profile('alpha', { total_return: null }),
          profile('bravo', { total_return: Number.NaN }),
          profile('charlie', { total_return: 0.01 }),
        ],
      }),
      'paper',
    );

    expect(summary.profilesConfigured).toBe(3);
    expect(summary.best).toEqual({ profileId: 'charlie', totalReturn: 0.01 });
    expect(summary.worst).toEqual({ profileId: 'charlie', totalReturn: 0.01 });
  });

  it('answers null extremes when no profile of the mode carries a return', () => {
    const summary = accountPerformance(
      payload({ profiles: [profile('alpha', { total_return: null })] }),
      'paper',
    );

    expect(summary.best).toBeNull();
    expect(summary.worst).toBeNull();
    expect(summary.profilesConfigured).toBe(1);
  });

  it('ranks only the profiles of the selected mode', () => {
    const summary = accountPerformance(
      payload({
        profiles: [
          profile('alpha', { total_return: 0.01 }),
          profile('live-1', { mode: 'live', total_return: 5 }),
        ],
      }),
      'paper',
    );

    expect(summary.best).toEqual({ profileId: 'alpha', totalReturn: 0.01 });
    expect(summary.profilesConfigured).toBe(1);
  });

  it('never falls back to the paper figures for the real mode', () => {
    const summary = accountPerformance(payload({ wallets: { paper: wallet(), live: null } }), 'live');

    expect(summary.ledger.portfolioValue).toBeNull();
    expect(summary.realizedPnl).toBeNull();
    expect(summary.unrealizedPnl).toBeNull();
    expect(summary.deployed).toBeNull();
    // The profiles still rank: the list is not the ledger.
    expect(summary.profilesConfigured).toBe(1);
    expect(summary.best).toEqual({ profileId: 'live-1', totalReturn: 0.01 });
  });

  it('answers null P&L when the ledger does not carry the fields', () => {
    const summary = accountPerformance(
      payload({
        wallets: {
          paper: wallet({ realized_pnl: null, unrealized_pnl: null, deployed: null }),
          live: null,
        },
      }),
      'paper',
    );

    expect(summary.realizedPnl).toBeNull();
    expect(summary.unrealizedPnl).toBeNull();
    expect(summary.deployed).toBeNull();
    expect(summary.ledger.portfolioValue).toBe(25750);
  });

  it('resolves the paper ledger of an older server without a `wallets` key', () => {
    const summary = accountPerformance(
      payload({ wallets: undefined, wallet: wallet() }),
      'paper',
    );

    expect(summary.ledger.portfolioValue).toBe(25750);
    expect(
      accountPerformance(payload({ wallets: undefined, wallet: wallet() }), 'live').ledger
        .portfolioValue,
    ).toBeNull();
  });
});
