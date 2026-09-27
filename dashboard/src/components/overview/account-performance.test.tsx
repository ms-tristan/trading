import { render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { EMPTY_PLACEHOLDER } from '@/lib/format';
import type { ProfileSnapshot, ProfilesPayload, RunMode, WalletSnapshot } from '@/lib/types';

import { AccountPerformance } from './account-performance';

/**
 * The paper ledger of the fixtures: `initial_balance` 25,000, portfolio value
 * 25,750 — so the account is up 750 USDT, which is +3.00%.
 */
const paperWallet: WalletSnapshot = {
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
};

/** The real ledger: deliberately different figures from the paper one. */
const liveWallet: WalletSnapshot = {
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
  updated_at: '2024-01-01T00:00:00+00:00',
  total_cash: 4900,
  positions_value: 300,
  total_portfolio_value: 5200,
};

function makeProfile(
  profileId: string,
  overrides: Partial<ProfileSnapshot> = {},
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

/** Two paper profiles (one running, one halted) and one live profile. */
const payload: ProfilesPayload = {
  profiles: [
    makeProfile('alpha', { total_return: 0.045 }),
    makeProfile('bravo', { status: 'halted', total_return: -0.02 }),
    makeProfile('live-1', { mode: 'live', total_return: 0.01 }),
  ],
  generated_at: '2024-01-01T00:00:00+00:00',
  wallet: paperWallet,
  wallets: { paper: paperWallet, live: liveWallet },
};

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.useFakeTimers();
  fetchMock = vi.fn(async () => {
    throw new TypeError('this component must never issue a request');
  });
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** Render the block for one mode and return the rendered container. */
function renderPerformance(mode: RunMode = 'paper', data: ProfilesPayload = payload) {
  return render(<AccountPerformance profiles={data} mode={mode} />);
}

/** The tile whose visible label is exactly `label`, within `scope`. */
function tile(label: string, scope: HTMLElement): HTMLElement {
  const tiles = Array.from(scope.querySelectorAll<HTMLElement>('div.rounded-card'));
  for (const candidate of tiles) {
    if (candidate.querySelector('span')?.textContent === label) {
      return candidate;
    }
  }
  throw new Error(`no tile labelled ${label}`);
}

/** The performance tiles of the hero block. */
function heroTiles(): HTMLElement {
  return screen.getByTestId('account-performance-tiles');
}

describe('AccountPerformance', () => {
  it('leads with the portfolio value of the active mode as the hero number', () => {
    renderPerformance('paper');

    const hero = screen.getByTestId('account-performance-hero');
    expect(within(hero).getByText('Total portfolio value')).toBeInTheDocument();
    expect(within(hero).getByText('$25,750.00')).toBeInTheDocument();
    expect(
      screen.getByRole('heading', { level: 2, name: 'Account performance — paper trading' }),
    ).toBeInTheDocument();
  });

  it('renders the total return in USDT together with the derived percentage', () => {
    renderPerformance('paper');

    const totalReturn = tile('Total return', heroTiles());
    expect(totalReturn).toHaveTextContent('+$750.00');
    expect(totalReturn).toHaveTextContent('+3.00%');
    expect(totalReturn).toHaveTextContent('Up');
  });

  it('renders both P&L, the deployed capital and the running count', () => {
    renderPerformance('paper');

    const tiles = heroTiles();
    expect(tile('Realized P&L', tiles)).toHaveTextContent('+$1,000.00');
    expect(tile('Unrealized P&L', tiles)).toHaveTextContent('-$250.00');
    expect(tile('Deployed capital', tiles)).toHaveTextContent('$4,500.00');
    expect(tile('Profiles running / configured', tiles)).toHaveTextContent('1 / 2');
  });

  it('names the best and the worst profile with their signed return', () => {
    renderPerformance('paper');

    const tiles = heroTiles();
    const best = tile('Best profile', tiles);
    expect(best).toHaveTextContent('+4.50%');
    expect(best).toHaveTextContent('alpha');

    const worst = tile('Worst profile', tiles);
    expect(worst).toHaveTextContent('-2.00%');
    expect(worst).toHaveTextContent('bravo');
  });

  it('reads the real mode out of the same payload, never the paper figures', () => {
    renderPerformance('live');

    expect(within(screen.getByTestId('account-performance-hero')).getByText('$5,200.00')).toBeInTheDocument();
    const totalReturn = tile('Total return', heroTiles());
    expect(totalReturn).toHaveTextContent('+$200.00');
    expect(totalReturn).toHaveTextContent('+4.00%');
    expect(tile('Profiles running / configured', heroTiles())).toHaveTextContent('1 / 1');
    expect(tile('Best profile', heroTiles())).toHaveTextContent('live-1');
    expect(
      screen.getByRole('heading', { level: 2, name: 'Account performance — real trading' }),
    ).toBeInTheDocument();
  });

  it('compares BOTH ledgers side by side without switching mode', () => {
    renderPerformance('paper');

    const ledgers = screen.getByTestId('account-performance-ledgers');
    expect(within(ledgers).getByText('Both ledgers')).toBeInTheDocument();

    const paper = within(ledgers).getByTestId('ledger-paper');
    // The ledger the hero figures belong to says so in words, not by colour.
    expect(within(paper).getByText('Paper ledger · current mode')).toBeInTheDocument();
    expect(tile('Portfolio value', paper)).toHaveTextContent('$25,750.00');
    expect(tile('Total return', paper)).toHaveTextContent('+$750.00');
    expect(tile('Total return', paper)).toHaveTextContent('+3.00%');

    const live = within(ledgers).getByTestId('ledger-live');
    expect(within(live).getByText('Real ledger')).toBeInTheDocument();
    expect(tile('Portfolio value', live)).toHaveTextContent('$5,200.00');
    expect(tile('Total return', live)).toHaveTextContent('+$200.00');
    expect(tile('Total return', live)).toHaveTextContent('+4.00%');
  });

  it('renders the em dash for every absent figure, never NaN or undefined', () => {
    const withoutLedgers: ProfilesPayload = {
      profiles: [makeProfile('alpha', { total_return: null })],
      generated_at: null,
      wallet: null,
      wallets: { paper: null, live: null },
    };
    renderPerformance('paper', withoutLedgers);

    const container = screen.getByTestId('account-performance');

    expect(within(screen.getByTestId('account-performance-hero')).getByText(EMPTY_PLACEHOLDER)).toBeInTheDocument();

    const tiles = heroTiles();
    for (const label of [
      'Total return',
      'Realized P&L',
      'Unrealized P&L',
      'Deployed capital',
      'Best profile',
      'Worst profile',
    ]) {
      const rendered = tile(label, tiles);
      expect(rendered).toHaveTextContent(EMPTY_PLACEHOLDER);
      expect(rendered.querySelector('p')?.textContent).toBe(EMPTY_PLACEHOLDER);
    }

    // The counts are numbers, never em dashes: one running profile is a fact
    // even when the ledger that funds it has not published a figure yet.
    expect(tile('Profiles running / configured', tiles)).toHaveTextContent('1 / 1');

    // Both ledgers are absent too, and each still renders its own em dashes.
    for (const mode of ['paper', 'live']) {
      const ledger = within(container).getByTestId(`ledger-${mode}`);
      expect(tile('Portfolio value', ledger)).toHaveTextContent(EMPTY_PLACEHOLDER);
      expect(tile('Total return', ledger)).toHaveTextContent(EMPTY_PLACEHOLDER);
    }

    const text = container.textContent ?? '';
    expect(text).not.toContain('NaN');
    expect(text).not.toContain('undefined');
    expect(text).not.toContain('Infinity');
  });

  it('renders the em dash percentage when the initial balance is unusable', () => {
    renderPerformance('paper', {
      ...payload,
      wallets: { paper: { ...paperWallet, initial_balance: 0 }, live: null },
    });

    // The value survives; the percentage is never invented from a zero balance.
    const totalReturn = tile('Total return', heroTiles());
    expect(totalReturn).toHaveTextContent(EMPTY_PLACEHOLDER);
    expect(tile('Portfolio value', screen.getByTestId('ledger-paper'))).toHaveTextContent(
      '$25,750.00',
    );
  });

  it('says so in words when no profile carries a return', () => {
    renderPerformance('paper', {
      profiles: [makeProfile('alpha', { total_return: null })],
      generated_at: null,
      wallet: paperWallet,
      wallets: { paper: paperWallet, live: null },
    });

    expect(tile('Best profile', heroTiles())).toHaveTextContent('No profile with a return yet');
    expect(tile('Worst profile', heroTiles())).toHaveTextContent('No profile with a return yet');
  });

  it('never leaves the best or worst caption empty', () => {
    renderPerformance('paper', {
      profiles: [makeProfile('', { total_return: 0.02 })],
      generated_at: null,
      wallet: paperWallet,
      wallets: { paper: paperWallet, live: null },
    });

    // The rank exists but its name does not: the caption falls back to the
    // shared em dash placeholder instead of rendering an empty cell.
    expect(tile('Best profile', heroTiles())).toHaveTextContent(EMPTY_PLACEHOLDER);
    expect(tile('Worst profile', heroTiles())).toHaveTextContent(EMPTY_PLACEHOLDER);
    expect(tile('Best profile', heroTiles())).toHaveTextContent('+2.00%');
  });

  it('re-reads the SAME payload on a mode change, with no request and no poll', () => {
    const view = renderPerformance('paper');
    expect(fetchMock).not.toHaveBeenCalled();

    view.rerender(<AccountPerformance profiles={payload} mode="live" />);

    expect(
      screen.getByRole('heading', { level: 2, name: 'Account performance — real trading' }),
    ).toBeInTheDocument();
    expect(within(screen.getByTestId('account-performance-hero')).getByText('$5,200.00')).toBeInTheDocument();

    // No second poll: nothing at all is scheduled by this component.
    vi.advanceTimersByTime(60000);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('pairs every trend with a text label and an icon, never colour alone', () => {
    renderPerformance('paper');

    const totalReturn = tile('Total return', heroTiles());
    expect(totalReturn).toHaveTextContent('Up');
    expect(totalReturn.querySelectorAll('svg').length).toBeGreaterThanOrEqual(1);

    const unrealized = tile('Unrealized P&L', heroTiles());
    expect(unrealized).toHaveTextContent('Down');
    expect(unrealized.querySelectorAll('svg').length).toBeGreaterThanOrEqual(1);
  });
});
