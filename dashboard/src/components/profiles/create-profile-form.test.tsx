import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { OPERATOR_TOKEN_STORAGE_KEY } from '@/lib/operator-token';
import type { CatalogPayload, CreateProfilePayload, ProfileSnapshot } from '@/lib/types';

import { CREATE_PROFILE_MESSAGES, CreateProfileForm } from './create-profile-form';

const { pushMock } = vi.hoisted(() => ({ pushMock: vi.fn() }));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: pushMock }),
}));

const CATALOG: CatalogPayload = {
  symbols: [
    { symbol: 'BTC/USDT', base: 'BTC', quote: 'USDT' },
    { symbol: 'ETH/USDT', base: 'ETH', quote: 'USDT' },
  ],
  strategies: ['basic'],
  timeframes: ['1m', '5m', '15m', '1h', '4h', '1d'],
  modes: ['paper', 'live'],
};

const CREATED_PROFILE: ProfileSnapshot = {
  profile_id: 'btc-paper',
  symbol: 'BTC/USDT',
  timeframe: '1h',
  strategy: 'basic',
  mode: 'paper',
  status: 'starting',
  initial_balance: 10000,
  equity: 10000,
  cash: 10000,
  position_value: 0,
  total_return: 0,
  n_trades: 0,
  open_positions: 0,
  health: {
    profile_id: 'btc-paper',
    status: 'starting',
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

const CREATED: CreateProfilePayload = { profile: CREATED_PROFILE };

const TOKEN = 'secret-operator-token';

interface RecordedCall {
  url: string;
  init: RequestInit | undefined;
}

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(JSON.stringify(body)),
  } as unknown as Response;
}

function errorResponse(status: number, message: string): Response {
  return jsonResponse({ error: message }, status);
}

/** A recording `fetch` seam: every call is captured, no network is reached. */
interface FetchSeam {
  calls: RecordedCall[];
  impl: typeof fetch;
}

function installFetch(response: Response | Error): FetchSeam {
  const calls: RecordedCall[] = [];
  const impl = (url: unknown, init?: RequestInit): Promise<Response> => {
    calls.push({ url: String(url), init });
    if (response instanceof Error) {
      return Promise.reject(response);
    }
    return Promise.resolve(response);
  };
  return { calls, impl: impl as unknown as typeof fetch };
}

/** Render the form with a recording fetch seam and return the recorded calls. */
function renderForm(
  response: Response | Error,
  options: { catalog?: CatalogPayload; operatorToken?: string } = {},
): RecordedCall[] {
  const seam = installFetch(response);
  render(
    <CreateProfileForm
      catalog={options.catalog ?? CATALOG}
      operatorToken={options.operatorToken ?? TOKEN}
      fetchImpl={seam.impl}
    />,
  );
  return seam.calls;
}

function combobox(name: string): HTMLInputElement {
  return screen.getByRole('combobox', { name }) as HTMLInputElement;
}

function submitButton(): HTMLButtonElement {
  return screen.getByRole('button', { name: /create profile|creating\.\.\./i }) as HTMLButtonElement;
}

/** Fill every required field with a valid value. */
async function fillValidForm(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  await user.type(screen.getByLabelText('Profile id'), 'btc-paper');
  await user.click(combobox('Asset'));
  await user.click(screen.getByRole('option', { name: /BTC\/USDT/ }));
  await user.click(combobox('Strategy'));
  await user.click(screen.getByRole('option', { name: 'basic' }));
}

beforeEach(() => {
  window.sessionStorage.clear();
  pushMock.mockReset();
  // No live server may ever be reached from a test.
  vi.stubGlobal(
    'fetch',
    vi.fn(() => {
      throw new TypeError('no live server in tests');
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.sessionStorage.clear();
});

describe('CreateProfileForm catalogue wiring', () => {
  it('feeds the asset combobox from catalog.symbols', async () => {
    const user = userEvent.setup();
    renderForm(jsonResponse(CREATED));

    await user.click(combobox('Asset'));

    expect(screen.getByRole('option', { name: /BTC\/USDT/ })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /ETH\/USDT/ })).toBeInTheDocument();
    expect(screen.getByText('BTC / USDT')).toBeInTheDocument();
  });

  it('feeds the strategy combobox from catalog.strategies, never a hard-coded list', async () => {
    const user = userEvent.setup();
    renderForm(jsonResponse(CREATED), {
      catalog: { ...CATALOG, strategies: ['basic', 'ema'] },
    });

    await user.click(combobox('Strategy'));

    expect(screen.getByRole('option', { name: 'basic' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'ema' })).toBeInTheDocument();
  });

  it('builds the timeframe select from catalog.timeframes and defaults to 1h', () => {
    renderForm(jsonResponse(CREATED));

    const select = screen.getByRole('combobox', { name: 'Timeframe' });
    expect(within(select).getAllByRole('option').map((option) => option.textContent)).toEqual([
      '1m',
      '5m',
      '15m',
      '1h',
      '4h',
      '1d',
    ]);
    expect(select).toHaveValue('1h');
  });

  it('defaults to the first offered timeframe when 1h is not supported', () => {
    renderForm(jsonResponse(CREATED), {
      catalog: { ...CATALOG, timeframes: ['15m', '4h'] },
    });

    expect(screen.getByRole('combobox', { name: 'Timeframe' })).toHaveValue('15m');
  });

  it('offers paper and live as a two-state choice, defaulting to paper', () => {
    renderForm(jsonResponse(CREATED));

    expect(screen.getByRole('radio', { name: 'paper' })).toBeChecked();
    expect(screen.getByRole('radio', { name: 'live' })).not.toBeChecked();
  });
});

describe('CreateProfileForm validation', () => {
  it('renders the exact message next to every empty field and sends no request', async () => {
    const user = userEvent.setup();
    const calls = renderForm(jsonResponse(CREATED));

    await user.click(submitButton());

    expect(screen.getByText(CREATE_PROFILE_MESSAGES.profileIdRequired)).toBeInTheDocument();
    expect(screen.getByText(CREATE_PROFILE_MESSAGES.symbolRequired)).toBeInTheDocument();
    expect(screen.getByText(CREATE_PROFILE_MESSAGES.strategyRequired)).toBeInTheDocument();

    const idInput = screen.getByLabelText('Profile id');
    expect(idInput).toHaveAttribute('aria-invalid', 'true');
    expect(idInput).toHaveAttribute('aria-describedby', 'create-profile-id-error');
    expect(idInput).toHaveFocus();

    expect(combobox('Asset')).toHaveAttribute('aria-describedby', 'create-profile-asset-error');
    expect(combobox('Strategy')).toHaveAttribute(
      'aria-describedby',
      'create-profile-strategy-error',
    );

    expect(calls).toHaveLength(0);
  });

  it('refuses a malformed profile id client-side', async () => {
    const user = userEvent.setup();
    const calls = renderForm(jsonResponse(CREATED));

    await user.type(screen.getByLabelText('Profile id'), 'bad id!');
    await user.click(submitButton());

    expect(screen.getByText(CREATE_PROFILE_MESSAGES.profileIdInvalid)).toBeInTheDocument();
    expect(calls).toHaveLength(0);
  });

  it('refuses the reserved profile id new client-side', async () => {
    const user = userEvent.setup();
    const calls = renderForm(jsonResponse(CREATED));

    await user.type(screen.getByLabelText('Profile id'), 'new');
    await user.click(submitButton());

    expect(screen.getByText(CREATE_PROFILE_MESSAGES.profileIdReserved)).toBeInTheDocument();
    expect(calls).toHaveLength(0);
  });

  it('refuses a non-positive initial balance client-side', async () => {
    const user = userEvent.setup();
    const calls = renderForm(jsonResponse(CREATED));

    const balance = screen.getByLabelText('Initial balance');
    await user.clear(balance);
    await user.type(balance, '0');
    await user.click(submitButton());

    expect(screen.getByText(CREATE_PROFILE_MESSAGES.balanceInvalid)).toBeInTheDocument();
    expect(balance).toHaveAttribute('aria-invalid', 'true');
    expect(calls).toHaveLength(0);
  });
});

describe('CreateProfileForm submission', () => {
  it('posts the exact body and the operator token, then navigates to the overview', async () => {
    const user = userEvent.setup();
    let resolveRequest: ((response: Response) => void) | undefined;
    const pendingResponse = new Promise<Response>((resolve) => {
      resolveRequest = resolve;
    });
    const calls: RecordedCall[] = [];
    const impl = (url: unknown, init?: RequestInit): Promise<Response> => {
      calls.push({ url: String(url), init });
      return pendingResponse;
    };
    render(
      <CreateProfileForm catalog={CATALOG} operatorToken={TOKEN} fetchImpl={impl as typeof fetch} />,
    );

    await fillValidForm(user);
    await user.click(submitButton());

    // While the request is in flight the form is busy and cannot be resubmitted.
    const busy = submitButton();
    expect(busy).toBeDisabled();
    expect(busy).toHaveAttribute('aria-busy', 'true');
    expect(busy).toHaveTextContent('Creating...');

    expect(calls).toHaveLength(1);
    const call = calls[0];
    expect(call?.url).toBe('/api/profiles');
    expect(call?.init?.method).toBe('POST');
    const headers = call?.init?.headers as Record<string, string>;
    expect(headers['Content-Type']).toBe('application/json');
    expect(headers['X-Operator-Token']).toBe(TOKEN);
    expect(JSON.parse(String(call?.init?.body))).toEqual({
      profile_id: 'btc-paper',
      symbol: 'BTC/USDT',
      timeframe: '1h',
      strategy: 'basic',
      mode: 'paper',
      initial_balance: 10000,
    });

    resolveRequest?.(jsonResponse(CREATED));

    await waitFor(() => {
      expect(pushMock).toHaveBeenCalledWith('/');
    });
    expect(screen.getByRole('status')).toHaveTextContent('Profile btc-paper created.');
    expect(submitButton()).not.toBeDisabled();
    expect(submitButton()).toHaveTextContent('Create profile');
    // The token is a header, never a rendered value.
    expect(document.body.textContent ?? '').not.toContain(TOKEN);
  });

  it('sends the selected mode and an explicit initial balance', async () => {
    const user = userEvent.setup();
    const calls = renderForm(jsonResponse(CREATED));

    await fillValidForm(user);
    await user.click(screen.getByRole('radio', { name: 'live' }));
    const balance = screen.getByLabelText('Initial balance');
    await user.clear(balance);
    await user.type(balance, '2500.5');
    await user.click(submitButton());

    await waitFor(() => {
      expect(calls).toHaveLength(1);
    });
    expect(JSON.parse(String(calls[0]?.init?.body))).toMatchObject({
      mode: 'live',
      initial_balance: 2500.5,
    });
  });

  it('omits the balance when the field is left empty', async () => {
    const user = userEvent.setup();
    const calls = renderForm(jsonResponse(CREATED));

    await fillValidForm(user);
    await user.clear(screen.getByLabelText('Initial balance'));
    await user.click(submitButton());

    await waitFor(() => {
      expect(calls).toHaveLength(1);
    });
    expect(JSON.parse(String(calls[0]?.init?.body))).not.toHaveProperty('initial_balance');
  });

  it('falls back to the operator token stored in sessionStorage', async () => {
    const user = userEvent.setup();
    window.sessionStorage.setItem(OPERATOR_TOKEN_STORAGE_KEY, TOKEN);
    const calls = renderForm(jsonResponse(CREATED), { operatorToken: '' });

    await fillValidForm(user);
    await user.click(submitButton());

    await waitFor(() => {
      expect(calls).toHaveLength(1);
    });
    const headers = calls[0]?.init?.headers as Record<string, string>;
    expect(headers['X-Operator-Token']).toBe(TOKEN);
  });
});

describe('CreateProfileForm failures', () => {
  it.each([
    [409, 'profile already exists: btc-paper'],
    [400, 'unknown strategy: nope (available: basic)'],
    [403, 'read-only mode: lifecycle routes are disabled'],
  ])('shows the HTTP %i message verbatim and keeps every entered value', async (status, message) => {
    const user = userEvent.setup();
    renderForm(errorResponse(status, message));

    await fillValidForm(user);
    await user.click(submitButton());

    await waitFor(() => {
      expect(screen.getByText(message)).toBeInTheDocument();
    });

    expect(submitButton()).not.toBeDisabled();
    expect(submitButton()).toHaveTextContent('Create profile');
    expect(screen.getByLabelText('Profile id')).toHaveValue('btc-paper');
    expect(combobox('Asset')).toHaveValue('BTC/USDT');
    expect(combobox('Strategy')).toHaveValue('basic');
    expect(pushMock).not.toHaveBeenCalled();
    expect(screen.queryByText(/created\./)).not.toBeInTheDocument();
  });

  it('reports a network failure explicitly', async () => {
    const user = userEvent.setup();
    renderForm(new TypeError('fetch failed'));

    await fillValidForm(user);
    await user.click(submitButton());

    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent(
        'network error calling /api/profiles: fetch failed',
      );
    });
    expect(submitButton()).not.toBeDisabled();
  });
});
