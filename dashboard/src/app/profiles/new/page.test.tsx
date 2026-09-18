import { render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// The form is a Client Component and reads the App Router instance.
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

// The page is an async Server Component: awaiting it runs the real fetch seam,
// so only the catalogue call is mocked. `errorMessage` stays real.
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    fetchCatalog: vi.fn(),
  };
});

import { ApiError, fetchCatalog } from '@/lib/api';
import type { CatalogPayload } from '@/lib/types';

import CreateProfilePage from './page';

const API_ORIGIN = 'http://monitor.test:8080';

const CATALOG: CatalogPayload = {
  symbols: [
    { symbol: 'BTC/USDT', base: 'BTC', quote: 'USDT' },
    { symbol: 'ETH/USDT', base: 'ETH', quote: 'USDT' },
  ],
  strategies: ['basic'],
  timeframes: ['15m', '1h', '4h'],
  modes: ['paper', 'live'],
};

beforeEach(() => {
  process.env.API_ORIGIN = API_ORIGIN;
  vi.mocked(fetchCatalog).mockResolvedValue(CATALOG);
  // No live server may ever be reached from a test.
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => {
      throw new TypeError('no live server in tests');
    }),
  );
});

afterEach(() => {
  delete process.env.API_ORIGIN;
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe('CreateProfilePage', () => {
  it('fetches the catalogue once with the server base URL and renders the form', async () => {
    render(await CreateProfilePage());

    expect(fetchCatalog).toHaveBeenCalledTimes(1);
    expect(fetchCatalog).toHaveBeenCalledWith({ baseUrl: API_ORIGIN });

    expect(screen.getByRole('heading', { level: 1, name: 'New profile' })).toBeInTheDocument();
    expect(screen.getByLabelText('Profile id')).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Asset' })).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Strategy' })).toBeInTheDocument();
    expect(screen.getByLabelText('Initial balance')).toHaveValue(10000);
  });

  it('builds the pickers from the fetched catalogue, never from a hard-coded list', async () => {
    render(await CreateProfilePage());

    const timeframe = screen.getByRole('combobox', { name: 'Timeframe' });
    expect(within(timeframe).getAllByRole('option').map((option) => option.textContent)).toEqual([
      '15m',
      '1h',
      '4h',
    ]);
    expect(timeframe).toHaveValue('1h');
  });

  it('renders the unreachable panel without throwing when the catalogue fails', async () => {
    vi.mocked(fetchCatalog).mockRejectedValue(
      new ApiError('network', 'network error calling /api/catalog: fetch failed', {
        path: '/api/catalog',
      }),
    );

    render(await CreateProfilePage());

    expect(screen.getByRole('status')).toHaveTextContent(
      'network error calling /api/catalog: fetch failed',
    );
    expect(screen.getByText('API unreachable')).toBeInTheDocument();
    expect(screen.getByText(/API_ORIGIN/)).toBeInTheDocument();
    expect(screen.getByText(new RegExp(API_ORIGIN))).toBeInTheDocument();

    // No form is rendered when its catalogue could not be read.
    expect(screen.queryByLabelText('Profile id')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /create profile/i })).not.toBeInTheDocument();
  });
});
