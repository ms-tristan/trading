import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { EMPTY_PLACEHOLDER } from '@/lib/format';
import { OPERATOR_TOKEN_STORAGE_KEY } from '@/lib/operator-token';
import type { ControlPayload, ProfileSnapshot, ProfileStatus } from '@/lib/types';

import { ProfileCard } from './profile-card';

/**
 * One profile snapshot exactly as `GET /api/profiles` emits it.
 *
 * The money fields are attributed shares of the shared platform wallet:
 * `allocation` 10,000 is the profile's share, `deployed` 2,245.50 is the capital
 * it has in the market, so `cash = allocation - deployed + realized_pnl`
 * (8,000 = 10,000 - 2,245.50 + 245.50) and
 * `equity = allocation + realized_pnl + unrealized_pnl`
 * (10,450.50 = 10,000 + 245.50 + 205).
 */
function makeProfile(overrides: Partial<ProfileSnapshot> = {}): ProfileSnapshot {
  return {
    profile_id: 'alpha',
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
      profile_id: 'alpha',
      status: 'running',
      last_candle_at: '2024-01-01T00:00:00+00:00',
      lag_seconds: 12.5,
      last_error: null,
      reconnect_count: 0,
      counters: {
        candles_processed: 120,
        orders_submitted: 4,
        orders_filled: 3,
        orders_rejected: 1,
        stream_reconnects: 0,
        risk_rejections: 1,
        errors: 0,
      },
    },
    started_at: '2023-12-01T00:00:00+00:00',
    updated_at: '2024-01-01T00:00:00+00:00',
    allocation: 10000,
    deployed: 2245.5,
    realized_pnl: 245.5,
    unrealized_pnl: 205,
    last_block_reason: null,
    ...overrides,
  };
}

const OPERATOR_TOKEN = 'card-operator-token';

/** `GET /api/control` answer of the card tests: only this profile. */
function controlPayload(profileId = 'alpha', paused = false): ControlPayload {
  return {
    engine_running: true,
    read_only: false,
    mutable: true,
    profiles: [{ profile_id: profileId, paused, running: true }],
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(JSON.stringify(body)),
  } as unknown as Response;
}

let fetchMock: ReturnType<typeof vi.fn>;

/** Let the first control poll of the actions island settle. */
async function settle(): Promise<void> {
  await act(async () => {
    for (let tick = 0; tick < 8; tick += 1) {
      await Promise.resolve();
    }
  });
}

/** Render the card and wait for the control poll of its actions island. */
async function renderCard(profile: ProfileSnapshot) {
  const view = render(<ProfileCard profile={profile} />);
  await settle();
  return view;
}

/** The heading of the card, which is also its accessible label. */
function heading(): HTMLElement {
  return screen.getByRole('heading', { level: 3 });
}

/** The card element itself, reached through its labelled heading. */
function card(): HTMLElement | null {
  return heading().closest('article');
}

/** The compact key-value row of the card (never a definition grid). */
function keyValues(): HTMLElement {
  return screen.getByTestId('profile-card-key-values');
}

/** The key-value pair whose `<dt>` text is exactly `label`. */
function keyValue(label: string): HTMLElement {
  const pairs = keyValues().querySelectorAll<HTMLElement>('[data-testid="profile-card-key-value"]');
  for (const pair of pairs) {
    if (pair.querySelector('dt')?.textContent === label) {
      return pair;
    }
  }
  throw new Error(`no key-value pair for ${label}`);
}

/** The `SYMBOL · TIMEFRAME` secondary line of the heading block. */
function symbolLine(): HTMLElement {
  return screen.getByTestId('profile-card-symbol');
}

/**
 * The labels that moved to the `/profiles/[id]` detail route (rendered there by
 * the profile header) and may therefore never appear on the overview card.
 */
const ANNEX_LABELS = [
  'Profile id',
  'Symbol',
  'Timeframe',
  'Strategy',
  'Attributed cash',
  'Allocation',
  'Deployed',
  'Position value',
  'Initial balance',
  'Realized P&L',
  'Unrealized P&L',
  'Last blocked order',
  'Trades',
  'Open positions',
  'Last candle',
  'Candle lag',
  'Started at',
  'Updated at',
] as const;

/**
 * The annex values of the default fixture: the figures the trimmed card no
 * longer renders (strategy, attributed cash, allocation, deployed, position
 * value, initial balance, realized and unrealized P&L, trade count, candle
 * timestamp, candle lag, start time).
 */
const ANNEX_VALUES = [
  'BasicStrategy',
  '$8,000.00',
  '$10,000.00',
  '$2,245.50',
  '$2,450.50',
  '+$245.50',
  '+$205.00',
  '2024-01-01 00:00:00 UTC',
  '12s',
  '2023-12-01 00:00:00 UTC',
] as const;

beforeEach(() => {
  window.sessionStorage.setItem(OPERATOR_TOKEN_STORAGE_KEY, OPERATOR_TOKEN);
  fetchMock = vi.fn(async (url: unknown): Promise<Response> => {
    if (String(url) === '/api/control') {
      return jsonResponse(controlPayload());
    }
    return jsonResponse({ error: 'not found' }, 404);
  });
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.sessionStorage.clear();
});

describe('ProfileCard', () => {
  it('renders only the essential values of the profile', async () => {
    await renderCard(makeProfile());

    expect(heading()).toHaveTextContent('alpha');
    expect(heading()).toHaveAttribute('id', 'profile-alpha');
    expect(symbolLine()).toHaveTextContent('BTC/USDT · 1h');

    expect(keyValue('Attributed equity')).toHaveTextContent('$10,450.50');
    expect(keyValue('Total return')).toHaveTextContent('+4.50%');
    expect(keyValue('Total return')).toHaveTextContent('Up');
  });

  it('never renders the annex fields on the overview card', async () => {
    await renderCard(makeProfile());

    for (const label of ANNEX_LABELS) {
      expect(screen.queryByText(label)).toBeNull();
    }

    for (const value of ANNEX_VALUES) {
      expect(screen.queryByText(value)).toBeNull();
    }

    const text = document.body.textContent ?? '';
    expect(text).not.toContain('BasicStrategy');
    expect(text).not.toContain('+$245.50');
    expect(text).not.toContain('+$205.00');

    // The retained figures keep their exact labels: equity stays *attributed*
    // (a share of the shared wallet) and is never renamed to a pot of its own.
    expect(screen.getByText('Attributed equity')).toBeInTheDocument();
    expect(screen.getByText('Total return')).toBeInTheDocument();
  });

  it('renders the em dash for every absent retained value and never NaN or undefined', async () => {
    await renderCard(
      makeProfile({
        profile_id: '',
        symbol: '',
        timeframe: null as unknown as string,
        equity: null,
        total_return: null,
        health: {
          profile_id: 'alpha',
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
      }),
    );

    // Both sides of the symbol line fall back to the em dash placeholder.
    expect(symbolLine()).toHaveTextContent(EMPTY_PLACEHOLDER);
    // The `<dd>` is the only place the figure can be: no stray text around it.
    expect(keyValue('Attributed equity').querySelector('dd')?.textContent).toBe(EMPTY_PLACEHOLDER);
    expect(keyValue('Total return')).toHaveTextContent(EMPTY_PLACEHOLDER);
    // An absent total return carries no trend claim at all: no text, no icon.
    expect(keyValue('Total return')).not.toHaveTextContent('Up');
    expect(keyValue('Total return')).not.toHaveTextContent('Down');
    expect(keyValue('Total return')).not.toHaveTextContent('Flat');
    expect(keyValue('Total return').querySelector('svg')).toBeNull();

    const text = document.body.textContent ?? '';
    expect(text).not.toContain('NaN');
    expect(text).not.toContain('undefined');
  });

  it('falls back to the em dash on either side of the symbol line independently', async () => {
    const { unmount } = await renderCard(makeProfile({ symbol: '' }));

    const withoutSymbol = symbolLine().textContent ?? '';
    expect(withoutSymbol.startsWith(EMPTY_PLACEHOLDER)).toBe(true);
    expect(withoutSymbol.endsWith('1h')).toBe(true);
    unmount();

    await renderCard(makeProfile({ timeframe: null as unknown as string }));

    const withoutTimeframe = symbolLine().textContent ?? '';
    expect(withoutTimeframe.startsWith('BTC/USDT')).toBe(true);
    expect(withoutTimeframe.endsWith(EMPTY_PLACEHOLDER)).toBe(true);
  });

  it('pairs every state with a tone, a text label and an icon', async () => {
    const { unmount } = await renderCard(makeProfile({ mode: 'live' }));

    const liveBadge = screen.getByText('Live').closest('span[data-tone]');
    expect(liveBadge).toHaveAttribute('data-tone', 'warn');
    expect(liveBadge?.querySelector('svg')).not.toBeNull();
    unmount();

    const second = await renderCard(makeProfile({ mode: 'paper', status: 'degraded' }));

    const paperBadge = screen.getByText('Paper').closest('span[data-tone]');
    expect(paperBadge).toHaveAttribute('data-tone', 'info');
    expect(paperBadge?.querySelector('svg')).not.toBeNull();

    const degradedBadge = screen.getByText('Degraded').closest('span[data-tone]');
    expect(degradedBadge).toHaveAttribute('data-tone', 'warn');
    expect(degradedBadge?.querySelector('svg')).not.toBeNull();
    second.unmount();

    await renderCard(makeProfile({ status: 'running' }));

    const runningBadge = screen.getByText('Running').closest('span[data-tone]');
    expect(runningBadge).toHaveAttribute('data-tone', 'ok');
    expect(runningBadge?.querySelector('svg')).not.toBeNull();
  });

  it('falls back to a neutral badge for a status outside the documented set', async () => {
    await renderCard(makeProfile({ status: 'unknown' as ProfileStatus }));

    const badge = screen.getByText('unknown').closest('span[data-tone]');
    expect(badge).toHaveAttribute('data-tone', 'neutral');
    expect(badge?.querySelector('svg')).not.toBeNull();
  });

  it('falls back to a neutral badge for a mode outside the documented set', async () => {
    await renderCard(makeProfile({ mode: 'sandbox' as ProfileSnapshot['mode'] }));

    const badge = screen.getByText('sandbox').closest('span[data-tone]');
    expect(badge).toHaveAttribute('data-tone', 'neutral');
    expect(badge?.querySelector('svg')).not.toBeNull();
  });

  it('pairs the total return with a sign, a trend label and an icon', async () => {
    const { unmount } = await renderCard(makeProfile({ total_return: 0.045 }));

    expect(keyValue('Total return')).toHaveTextContent('+4.50%');
    expect(keyValue('Total return')).toHaveTextContent('Up');
    expect(keyValue('Total return').querySelectorAll('svg')).toHaveLength(1);
    unmount();

    await renderCard(makeProfile({ total_return: -0.02 }));

    expect(keyValue('Total return')).toHaveTextContent('-2.00%');
    expect(keyValue('Total return')).toHaveTextContent('Down');
    expect(keyValue('Total return').querySelectorAll('svg')).toHaveLength(1);
  });

  it('labels the card with its heading and links it to the profile detail route', async () => {
    await renderCard(makeProfile({ profile_id: 'alpha beta' }));

    expect(heading()).toHaveAttribute('id', 'profile-alpha-beta');

    const link = screen.getByRole('link', { name: 'alpha beta' });
    expect(link).toHaveAttribute('href', '/profiles/alpha%20beta');
  });

  it('stretches the profile link over the whole card', async () => {
    await renderCard(makeProfile());

    // The heading link carries the overlay that makes the entire card a target,
    // and the card is the positioning context it stretches over. Without both,
    // only the few characters of the profile id were clickable and the rest of
    // the card looked interactive but did nothing.
    const link = screen.getByRole('link', { name: 'alpha' });
    expect(link.className).toContain('after:absolute');
    expect(link.className).toContain('after:inset-0');

    expect(link.closest('article')?.className).toContain('relative');
  });

  it('keeps the lifecycle controls above the stretched link', async () => {
    await renderCard(makeProfile());

    // The buttons are lifted above the overlay, so clicking them still controls
    // the profile instead of navigating away.
    const pause = screen.getByRole('button', { name: 'Pause' });
    const footer = pause.closest('footer');
    expect(footer?.className).toContain('z-10');
    expect(footer?.className).toContain('relative');
  });

  it('surfaces a non-null last error as a labelled warning row', async () => {
    const { unmount } = await renderCard(
      makeProfile({
        health: {
          ...makeProfile().health,
          status: 'degraded',
          last_error: 'feed disconnected',
        },
      }),
    );

    const row = screen.getByText('Last error').closest('p');
    expect(row).toHaveTextContent('feed disconnected');
    expect(row?.querySelector('svg')).not.toBeNull();
    unmount();

    await renderCard(makeProfile());
    expect(screen.queryByText('Last error')).toBeNull();
  });

  it('renders a compact card without a definition grid', async () => {
    await renderCard(makeProfile());

    // The card holds exactly one `<dl>` — the compact key-value row — and it
    // holds exactly the two retained figures: the 19-field grid is gone.
    expect(card()?.querySelector('dl')).toBe(keyValues());
    expect(keyValues().querySelectorAll('[data-testid="profile-card-key-value"]')).toHaveLength(2);
    expect(screen.queryByText('Strategy')).toBeNull();
  });

  it('renders the actions island of the profile in the card footer', async () => {
    await renderCard(makeProfile());

    expect(screen.getByRole('button', { name: 'Pause' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Resume' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Delete profile' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Pause' }).closest('[data-paused]')).toHaveAttribute(
      'data-paused',
      'false',
    );
    expect(fetchMock).toHaveBeenCalledWith('/api/control', expect.anything());
  });

  it('shows the card as paused when /api/control reports the profile paused', async () => {
    fetchMock.mockImplementation(async (url: unknown): Promise<Response> => {
      if (String(url) === '/api/control') {
        return jsonResponse(controlPayload('alpha', true));
      }
      return jsonResponse({ error: 'not found' }, 404);
    });

    await renderCard(makeProfile());

    expect(screen.getByText('Paused').closest('span[data-tone]')).toHaveAttribute(
      'data-tone',
      'warn',
    );
    expect(
      screen.getByText('Not opening new positions; the open position stays managed.'),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Resume' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Pause' })).toBeDisabled();
  });
});

describe('ProfileCard press feedback', () => {
  it('acknowledges a press on the stretched link', async () => {
    await renderCard(makeProfile());

    const link = screen.getByRole('link', { name: 'alpha' });
    expect(link).toHaveClass('active:text-accent');
    expect(link).toHaveClass('active:decoration-accent');
    // The hover styling and the stretched overlay are untouched: the press
    // state adds no element and no size, so nothing can reflow.
    expect(link).toHaveClass('hover:text-accent');
    expect(link).toHaveClass('hover:decoration-accent');
    expect(link).toHaveClass('after:absolute');
    expect(link).toHaveClass('after:inset-0');
    expect(link.className).toContain('after:content-[""]');
    expect(link).toHaveClass('motion-safe:duration-200');
  });

  it('lights the card up while the stretched link is pressed, and only then', async () => {
    await renderCard(makeProfile());

    const link = screen.getByRole('link', { name: 'alpha' });
    const element = link.closest('article') as HTMLElement;

    // Scoped to `a:active`: pressing a button of the footer never puts the link
    // in the active state, so the card cannot light up for a lifecycle click.
    expect(element).toHaveClass('has-[a:active]:border-accent');
    expect(element).toHaveClass('has-[a:active]:ring-1');
    expect(element).toHaveClass('has-[a:active]:ring-accent');
    // A ring is a box-shadow: no reflow, and the existing states stay intact.
    expect(element).toHaveClass('hover:border-accent/50');
    expect(element).toHaveClass('focus-within:border-accent/50');
    expect(element).toHaveClass('motion-safe:transition-colors');
    expect(element).toHaveClass('motion-safe:duration-200');
  });

  it('never lets the card press affordance swallow a lifecycle click', async () => {
    await renderCard(makeProfile());
    fetchMock.mockClear();

    await userEvent.click(screen.getByRole('button', { name: 'Pause' }));
    await settle();

    // The handler of the footer button ran: the click reached the button, not
    // the stretched link that covers the rest of the card.
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/profiles/alpha/pause',
      expect.objectContaining({ method: 'POST' }),
    );
  });

  it('keeps the delete action reachable from the footer', async () => {
    await renderCard(makeProfile());

    await userEvent.click(screen.getByRole('button', { name: 'Delete profile' }));

    // The first click opens the confirmation instead of navigating away: the
    // pressed affordance of the card never captures it.
    expect(screen.getByRole('dialog')).toHaveTextContent('Delete profile alpha?');
  });
});
