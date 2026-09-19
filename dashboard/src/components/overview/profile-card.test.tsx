import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { EMPTY_PLACEHOLDER } from '@/lib/format';
import { OPERATOR_TOKEN_STORAGE_KEY } from '@/lib/operator-token';
import type { ControlPayload, ProfileSnapshot, ProfileStatus } from '@/lib/types';

import { ProfileCard } from './profile-card';

/** One profile snapshot exactly as `GET /api/profiles` emits it. */
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
    ...overrides,
  };
}

/** The labelled field wrapper of `label` (its `<dt>` / `<dd>` pair). */
function field(label: string): HTMLElement {
  const wrapper = screen.getByText(label).closest('div');
  if (wrapper === null) {
    throw new Error(`no field wrapper for ${label}`);
  }
  return wrapper;
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
  it('renders every labelled field of the profile', async () => {
    await renderCard(makeProfile());

    expect(field('Profile id')).toHaveTextContent('alpha');
    expect(field('Symbol')).toHaveTextContent('BTC/USDT');
    expect(field('Timeframe')).toHaveTextContent('1h');
    expect(field('Strategy')).toHaveTextContent('BasicStrategy');
    expect(field('Equity')).toHaveTextContent('$10,450.50');
    expect(field('Cash')).toHaveTextContent('$8,000.00');
    expect(field('Position value')).toHaveTextContent('$2,450.50');
    expect(field('Initial balance')).toHaveTextContent('$10,000.00');
    expect(field('Trades')).toHaveTextContent('12');
    expect(field('Open positions')).toHaveTextContent('1');
    expect(field('Last candle')).toHaveTextContent('2024-01-01 00:00:00 UTC');
    expect(field('Candle lag')).toHaveTextContent('12s');
    expect(field('Started at')).toHaveTextContent('2023-12-01 00:00:00 UTC');
    expect(field('Updated at')).toHaveTextContent('2024-01-01 00:00:00 UTC');
  });

  it('labels the card with its heading and links it to the profile detail route', async () => {
    await renderCard(makeProfile({ profile_id: 'alpha beta' }));

    const heading = screen.getByRole('heading', { level: 3 });
    expect(heading).toHaveAttribute('id', 'profile-alpha-beta');

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

    const card = link.closest('article');
    expect(card?.className).toContain('relative');
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

  it('renders the mode and the status as text labels, never colour alone', async () => {
    const { unmount } = await renderCard(makeProfile({ mode: 'live' }));

    const liveBadge = screen.getByText('Live').closest('span[data-tone]');
    expect(liveBadge).toHaveAttribute('data-tone', 'warn');
    expect(liveBadge?.querySelector('svg')).not.toBeNull();
    unmount();

    await renderCard(makeProfile({ status: 'degraded', mode: 'paper' }));

    expect(screen.getByText('Paper').closest('span[data-tone]')).toHaveAttribute('data-tone', 'info');
    const degradedBadge = screen.getByText('Degraded').closest('span[data-tone]');
    expect(degradedBadge).toHaveAttribute('data-tone', 'warn');
    expect(degradedBadge?.querySelector('svg')).not.toBeNull();
  });

  it('pairs the total return with an explicit sign, a trend label and an icon', async () => {
    const { unmount } = await renderCard(makeProfile({ total_return: 0.045 }));

    expect(field('Total return')).toHaveTextContent('+4.50%');
    expect(field('Total return')).toHaveTextContent('Up');
    expect(field('Total return').querySelector('svg')).not.toBeNull();
    unmount();

    await renderCard(makeProfile({ total_return: -0.02 }));

    expect(field('Total return')).toHaveTextContent('-2.00%');
    expect(field('Total return')).toHaveTextContent('Down');
  });

  it('renders the em dash for every absent value and never NaN or undefined', async () => {
    await renderCard(
      makeProfile({
        symbol: '',
        timeframe: null as unknown as string,
        strategy: undefined as unknown as string,
        initial_balance: null,
        equity: null,
        cash: null,
        position_value: null,
        total_return: null,
        started_at: null,
        updated_at: null,
        n_trades: null as unknown as number,
        open_positions: undefined as unknown as number,
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

    for (const label of [
      'Symbol',
      'Timeframe',
      'Strategy',
      'Initial balance',
      'Equity',
      'Cash',
      'Position value',
      'Total return',
      'Trades',
      'Open positions',
      'Last candle',
      'Candle lag',
      'Started at',
      'Updated at',
    ]) {
      expect(field(label)).toHaveTextContent(EMPTY_PLACEHOLDER);
    }

    expect(screen.getAllByText(EMPTY_PLACEHOLDER).length).toBeGreaterThanOrEqual(14);
    const text = document.body.textContent ?? '';
    expect(text).not.toContain('NaN');
    expect(text).not.toContain('undefined');
    // An absent total return carries no trend claim at all.
    expect(field('Total return')).not.toHaveTextContent('Flat');
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
    expect(screen.queryByText('Last error')).not.toBeInTheDocument();
  });

  it('falls back to a neutral badge for a status outside the documented set', async () => {
    await renderCard(makeProfile({ status: 'unknown' as ProfileStatus }));

    const badge = screen.getByText('unknown').closest('span[data-tone]');
    expect(badge).toHaveAttribute('data-tone', 'neutral');
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
    const card = link.closest('article') as HTMLElement;

    // Scoped to `a:active`: pressing a button of the footer never puts the link
    // in the active state, so the card cannot light up for a lifecycle click.
    expect(card).toHaveClass('has-[a:active]:border-accent');
    expect(card).toHaveClass('has-[a:active]:ring-1');
    expect(card).toHaveClass('has-[a:active]:ring-accent');
    // A ring is a box-shadow: no reflow, and the existing states stay intact.
    expect(card).toHaveClass('hover:border-accent/50');
    expect(card).toHaveClass('focus-within:border-accent/50');
    expect(card).toHaveClass('motion-safe:transition-colors');
    expect(card).toHaveClass('motion-safe:duration-200');
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
