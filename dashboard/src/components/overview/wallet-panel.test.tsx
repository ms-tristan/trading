import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { EMPTY_PLACEHOLDER } from '@/lib/format';
import type { WalletSnapshot } from '@/lib/types';

import { WalletPanel } from './wallet-panel';

// ---------------------------------------------------------------------------
// fixtures: the shared wallet exactly as the frozen contract emits it
// ---------------------------------------------------------------------------

/**
 * A coherent paper wallet: `cash = initial_balance - deployed + realized_pnl`
 * (20,964.75 = 25,000 - 4,450.50 + 415.25) and
 * `equity = initial_balance + realized_pnl + unrealized_pnl`
 * (25,380.50 = 25,000 + 415.25 - 34.75).
 */
const wallet: WalletSnapshot = {
  name: 'usdt',
  mode: 'paper',
  initial_balance: 25000,
  cash: 20964.75,
  equity: 25380.5,
  deployed: 4450.5,
  realized_pnl: 415.25,
  unrealized_pnl: -34.75,
  total_exposure: 4450.5,
  profiles: 2,
  source: 'local',
  updated_at: '2024-01-01T00:00:00+00:00',
};

/** The documented value of every tile, in rendering order. */
const EXPECTED_TILES: ReadonlyArray<readonly [string, string]> = [
  ['Initial balance', '$25,000.00'],
  ['Cash', '$20,964.75'],
  ['Equity', '$25,380.50'],
  ['Deployed', '$4,450.50'],
  ['Total exposure', '$4,450.50'],
  ['Realized P&L', '+$415.25'],
  ['Unrealized P&L', '-$34.75'],
  ['Profiles funded', '2'],
];

const LABELS = EXPECTED_TILES.map(([label]) => label);

/** The tile grid (the values are also repeated in the table fallback). */
function tiles(): HTMLElement {
  return screen.getByTestId('wallet-tiles');
}

/**
 * One stat tile of the grid, resolved through its label.
 *
 * `StatTile` renders the label and the value as siblings of one tile root, so
 * the root is the grandparent of the label text.
 */
function tile(label: string): HTMLElement {
  const root = within(tiles()).getByText(label).closest('div')?.parentElement;
  if (root === null || root === undefined) {
    throw new Error(`no tile for ${label}`);
  }
  return root;
}

/** The accessible table fallback of the eight values. */
function valuesTable(): HTMLElement {
  return screen.getByRole('table', { name: /platform wallet/i });
}

describe('WalletPanel', () => {
  it('states that one shared wallet funds every profile and labels its eight values', () => {
    render(<WalletPanel wallet={wallet} />);

    // The wording is the point of the panel: one ledger, attributed shares.
    expect(
      screen.getByText(/one shared usdt wallet funds every order of every profile/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/attributed shares of that single ledger/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/never holds a pot of its own/i)).toBeInTheDocument();

    for (const [label, value] of EXPECTED_TILES) {
      expect(tile(label)).toHaveTextContent(value);
    }

    // The wallet identifies itself and stamps its last update, both labelled.
    expect(screen.getByText('Wallet').nextElementSibling).toHaveTextContent('usdt');
    expect(screen.getByText('Updated').nextElementSibling).toHaveTextContent(
      '2024-01-01 00:00:00 UTC',
    );
  });

  it('pairs every P&L with its sign, a trend label and an icon', () => {
    render(<WalletPanel wallet={wallet} />);

    expect(tile('Realized P&L')).toHaveTextContent('+$415.25');
    expect(tile('Realized P&L')).toHaveTextContent('Up');

    expect(tile('Unrealized P&L')).toHaveTextContent('-$34.75');
    expect(tile('Unrealized P&L')).toHaveTextContent('Down');

    // Colour is never the only signal: a glyph carries the direction too.
    expect(tile('Unrealized P&L').querySelector('svg')).not.toBeNull();
  });

  it('names the section and reflows its tiles from one to four columns', () => {
    render(<WalletPanel wallet={wallet} />);

    const region = screen.getByRole('region', { name: 'Platform wallet' });
    expect(region).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 2, name: 'Platform wallet' })).toBeInTheDocument();

    // 375 px: one column, 768 px (`sm`): two, 1440 px (`xl`): four.
    expect(tiles()).toHaveClass('grid', 'sm:grid-cols-2', 'xl:grid-cols-4');
  });

  it('renders the source and the mode as text badges with an icon, never colour alone', () => {
    const { unmount } = render(<WalletPanel wallet={wallet} />);

    const localBadge = screen.getByText('Local ledger').closest('span[data-tone]');
    expect(localBadge).toHaveAttribute('data-tone', 'info');
    expect(localBadge?.querySelector('svg')).not.toBeNull();

    const paperBadge = screen.getByText('Paper mode').closest('span[data-tone]');
    expect(paperBadge).toHaveAttribute('data-tone', 'info');
    expect(paperBadge?.querySelector('svg')).not.toBeNull();
    unmount();

    render(<WalletPanel wallet={{ ...wallet, source: 'venue', mode: 'live' }} />);

    const venueBadge = screen.getByText('Venue account').closest('span[data-tone]');
    expect(venueBadge).toHaveAttribute('data-tone', 'warn');
    expect(venueBadge?.querySelector('svg')).not.toBeNull();

    const liveBadge = screen.getByText('Live mode').closest('span[data-tone]');
    expect(liveBadge).toHaveAttribute('data-tone', 'warn');
    expect(liveBadge?.querySelector('svg')).not.toBeNull();
  });

  it('says that a live wallet mirrors the venue and is never debited locally', () => {
    const { unmount } = render(
      <WalletPanel wallet={{ ...wallet, mode: 'live', source: 'venue' }} />,
    );

    expect(screen.getByText(/mirrors the balance of the venue account/i)).toBeInTheDocument();
    expect(screen.getByText(/never debits it locally/i)).toBeInTheDocument();
    unmount();

    // A paper wallet funds orders from the local ledger: no venue claim at all.
    render(<WalletPanel wallet={wallet} />);
    expect(screen.queryByText(/never debits it locally/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/venue account/i)).not.toBeInTheDocument();
  });

  it('repeats the eight values in an accessible table with a caption', () => {
    render(<WalletPanel wallet={wallet} />);

    const table = valuesTable();
    // The caption is the accessible name of the table: the text fallback is in
    // the accessibility tree, not only in the tiles.
    expect(table.querySelector('caption')).toHaveTextContent(/platform wallet/i);
    expect(within(table).getByRole('columnheader', { name: 'Wallet value' })).toBeInTheDocument();
    expect(within(table).getByRole('columnheader', { name: 'Amount' })).toBeInTheDocument();

    // One header row plus one row per value.
    expect(within(table).getAllByRole('row')).toHaveLength(LABELS.length + 1);

    for (const [label, value] of EXPECTED_TILES) {
      const row = within(table).getByText(label).closest('tr');
      expect(row).toHaveTextContent(label);
      expect(row).toHaveTextContent(value);
    }
  });

  it('renders the em dash for every absent value and never NaN or undefined', () => {
    render(
      <WalletPanel
        wallet={{
          ...wallet,
          name: '   ',
          initial_balance: null,
          cash: null,
          equity: null,
          deployed: null,
          realized_pnl: null,
          unrealized_pnl: null,
          total_exposure: null,
          // The count is a number in the contract: `null` can only arrive from a
          // producer that omitted it, and it must render as an em dash too.
          profiles: null as unknown as number,
          updated_at: null,
        }}
      />,
    );

    expect(within(tiles()).getAllByText(EMPTY_PLACEHOLDER)).toHaveLength(LABELS.length);
    expect(screen.getByText('Wallet').nextElementSibling).toHaveTextContent(EMPTY_PLACEHOLDER);
    expect(screen.getByText('Updated').nextElementSibling).toHaveTextContent(EMPTY_PLACEHOLDER);

    // An absent P&L carries no trend claim: neither a glyph nor a label.
    expect(tile('Realized P&L')).not.toHaveTextContent('Flat');
    expect(tile('Unrealized P&L')).not.toHaveTextContent('Flat');

    const text = document.body.textContent ?? '';
    expect(text).not.toContain('NaN');
    expect(text).not.toContain('undefined');
  });

  it('renders the em dash state and a note when the platform reported no wallet', () => {
    render(<WalletPanel wallet={null} />);

    // Never a blank panel, never a thrown error.
    const note = screen.getByText('No shared wallet reported').closest('p');
    expect(note).toHaveTextContent(/no wallet snapshot/i);
    expect(within(tiles()).getAllByText(EMPTY_PLACEHOLDER)).toHaveLength(LABELS.length);

    // Both badges fall back to the em dash with the neutral tone.
    const badges = document.querySelectorAll<HTMLElement>('span[data-tone]');
    expect(badges).toHaveLength(2);
    for (const badge of badges) {
      expect(badge).toHaveAttribute('data-tone', 'neutral');
      expect(badge).toHaveTextContent(EMPTY_PLACEHOLDER);
      expect(badge.querySelector('svg')).not.toBeNull();
    }

    // No source and no mode are claimed, and no live-mode sentence is shown.
    expect(screen.queryByText('Local ledger')).not.toBeInTheDocument();
    expect(screen.queryByText('Venue account')).not.toBeInTheDocument();
    expect(screen.queryByText(/never debits it locally/i)).not.toBeInTheDocument();

    // The table fallback is rendered in that state too.
    expect(valuesTable()).toBeInTheDocument();
    expect(within(valuesTable()).getAllByRole('row')).toHaveLength(LABELS.length + 1);
  });

  it('uses no named max-w-* / min-w-* utility anywhere, which density tokens would collapse', () => {
    const { container } = render(<WalletPanel wallet={wallet} />);

    // `--spacing-*` shadows the container scale in Tailwind v4 (pinned by
    // `src/app/globals.test.ts`): a named width class would collapse the panel.
    expect(container.innerHTML).not.toMatch(/\b(?:max-w|min-w)-[a-z]/);
  });
});
