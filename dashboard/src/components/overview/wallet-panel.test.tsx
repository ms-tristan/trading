import { fireEvent, render, screen, within } from '@testing-library/react';
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
 *
 * The three totals are mutually coherent too:
 * `total_portfolio_value = total_cash + positions_value`
 * (25,380.50 = 20,964.75 + 4,415.75).
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
  total_cash: 20964.75,
  positions_value: 4415.75,
  total_portfolio_value: 25380.5,
};

/** The three primary totals, in rendering order, with their exact labels. */
const EXPECTED_TOTALS: ReadonlyArray<readonly [string, string]> = [
  ['Total available cash', '$20,964.75'],
  ['Total value of the positions', '$4,415.75'],
  ['Total value of the portfolio', '$25,380.50'],
];

/** The eight detailed values, now folded behind the disclosure. */
const EXPECTED_DETAILS: ReadonlyArray<readonly [string, string]> = [
  ['Initial balance', '$25,000.00'],
  ['Cash', '$20,964.75'],
  ['Equity', '$25,380.50'],
  ['Deployed', '$4,450.50'],
  ['Total exposure', '$4,450.50'],
  ['Realized P&L', '+$415.25'],
  ['Unrealized P&L', '-$34.75'],
  ['Profiles funded', '2'],
];

const TOTAL_LABELS = EXPECTED_TOTALS.map(([label]) => label);
const DETAIL_LABELS = EXPECTED_DETAILS.map(([label]) => label);

/** The primary tile grid (the three totals, and nothing else). */
function tiles(): HTMLElement {
  return screen.getByTestId('wallet-tiles');
}

/**
 * One stat tile of a grid, resolved through its label.
 *
 * `StatTile` renders the label and the value as siblings of one tile root, so
 * the root is the grandparent of the label text.
 */
function tile(root: HTMLElement, label: string): HTMLElement {
  const found = within(root).getByText(label).closest('div')?.parentElement;
  if (found === null || found === undefined) {
    throw new Error(`no tile for ${label}`);
  }
  return found;
}

/** The collapsed-by-default disclosure of the detailed values. */
function disclosure(): HTMLDetailsElement {
  const found = document.querySelector<HTMLDetailsElement>('details');
  if (found === null) {
    throw new Error('no disclosure');
  }
  return found;
}

/** The accessible table fallback of the eight detailed values. */
function valuesTable(): HTMLElement {
  return screen.getByRole('table', { name: /platform wallet/i });
}

/** Open the disclosure the way an operator does: a real click on its summary. */
function showDetails(): void {
  fireEvent.click(screen.getByText('Show details'));
}

/** The `summary` control of the disclosure, resolved through its role. */
function disclosureToggle(): HTMLElement {
  const summary = disclosure().querySelector('summary');
  if (summary === null) {
    throw new Error('no summary');
  }
  return summary;
}

describe('WalletPanel', () => {
  it('states that one shared wallet funds every profile and names the mode in words', () => {
    render(<WalletPanel wallet={wallet} mode="paper" />);

    // The wording is the point of the panel: one ledger, attributed shares.
    expect(
      screen.getByText(/one shared usdt wallet funds every order of every profile/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/attributed shares of that single ledger/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/never holds a pot of its own/i)).toBeInTheDocument();

    // The heading names the mode: a paper total can never be read as real money.
    expect(
      screen.getByRole('heading', { level: 2, name: 'Platform wallet — paper trading' }),
    ).toBeInTheDocument();
  });

  it('renders exactly the three primary totals, in order, with their exact labels', () => {
    render(<WalletPanel wallet={wallet} mode="paper" />);

    // The primary block holds the three totals and nothing else.
    const options = within(tiles()).getAllByText(/^Total /);
    expect(options.map((node) => node.textContent)).toEqual(TOTAL_LABELS);

    for (const [label, value] of EXPECTED_TOTALS) {
      expect(tile(tiles(), label)).toHaveTextContent(value);
    }
  });

  it('emphasises the portfolio total as THE total without hiding it from assistive tech', () => {
    render(<WalletPanel wallet={wallet} mode="paper" />);

    const primary = tile(tiles(), 'Total value of the portfolio');

    // A distinct treatment: an accent border and a larger value than its peers.
    expect(primary).toHaveClass('border-accent');
    expect(within(primary).getByText('$25,380.50')).toHaveClass('text-2xl');

    // Emphasis is decoration, never the meaning: the label, the value and the
    // icon are all still there, and the first two totals stay ordinary tiles.
    expect(within(primary).getByText('Total value of the portfolio')).toBeInTheDocument();
    expect(primary.querySelector('svg')).not.toBeNull();
    expect(tile(tiles(), 'Total available cash')).not.toHaveClass('border-accent');
    expect(tile(tiles(), 'Total available cash')).toHaveTextContent('$20,964.75');
  });

  it('folds the eight detailed values into a disclosure that starts COLLAPSED', () => {
    render(<WalletPanel wallet={wallet} mode="paper" />);

    // Collapsed on the first render of every case: the closed disclosure hides
    // its own content natively, so nothing of it is visible before the click.
    const details = disclosure();
    expect(details.open).toBe(false);

    // The visible text of the control is exactly "Show details" while closed,
    // and exactly "Hide details" once open — one sentence in the DOM at a time.
    const toggle = disclosureToggle();
    expect(toggle).toHaveTextContent('Show details');
    expect(toggle).not.toHaveTextContent('Hide details');
    expect(screen.getByText('Show details')).toBeVisible();

    for (const [label] of EXPECTED_DETAILS) {
      // Scoped to the tiles of the disclosure: each label is repeated once more
      // in its table fallback, which is the point of that fallback.
      expect(within(screen.getByTestId('wallet-details')).getByText(label)).not.toBeVisible();
    }

    showDetails();

    expect(disclosure().open).toBe(true);
    expect(disclosureToggle()).toHaveTextContent('Hide details');
    expect(disclosureToggle()).not.toHaveTextContent('Show details');
    expect(screen.getByText('Hide details')).toBeVisible();
    for (const [label, value] of EXPECTED_DETAILS) {
      expect(tile(screen.getByTestId('wallet-details'), label)).toHaveTextContent(value);
    }
  });

  it('keeps the wallet identity and the updated stamp inside the disclosure', () => {
    render(<WalletPanel wallet={wallet} mode="paper" />);

    // Moved behind the disclosure — not deleted: the header is the three totals.
    // Collapsed, the whole block is invisible; opened, both values are readable.
    expect(screen.getByText('Wallet')).not.toBeVisible();
    showDetails();

    expect(screen.getByText('Wallet')).toBeVisible();
    expect(screen.getByText('Wallet').nextElementSibling).toHaveTextContent('usdt');
    expect(screen.getByText('Updated').nextElementSibling).toHaveTextContent(
      '2024-01-01 00:00:00 UTC',
    );
  });

  it('pairs every P&L with its sign, a trend label and an icon, inside the disclosure', () => {
    render(<WalletPanel wallet={wallet} mode="paper" />);
    showDetails();

    const details = disclosure();
    expect(details.open).toBe(true);
    // Scoped to the tiles of the disclosure: the same labels are repeated once
    // more in its table fallback, which is the point of that fallback.
    const grid = screen.getByTestId('wallet-details');

    expect(tile(grid, 'Realized P&L')).toHaveTextContent('+$415.25');
    expect(tile(grid, 'Realized P&L')).toHaveTextContent('Up');

    expect(tile(grid, 'Unrealized P&L')).toHaveTextContent('-$34.75');
    expect(tile(grid, 'Unrealized P&L')).toHaveTextContent('Down');

    // Colour is never the only signal: a glyph carries the direction too.
    expect(tile(grid, 'Unrealized P&L').querySelector('svg')).not.toBeNull();
  });

  it('names the section and reflows its three totals from one to three columns', () => {
    render(<WalletPanel wallet={wallet} mode="paper" />);

    const region = screen.getByRole('region', { name: 'Platform wallet — paper trading' });
    expect(region).toBeInTheDocument();
    expect(
      screen.getByRole('heading', { level: 2, name: 'Platform wallet — paper trading' }),
    ).toBeInTheDocument();

    // 375 px: one column, 768 px (`sm`): two, 1440 px (`xl`): three.
    expect(tiles()).toHaveClass('grid', 'sm:grid-cols-2', 'xl:grid-cols-3');
  });

  it('renders the source and the mode as text badges with an icon, never colour alone', () => {
    const { unmount } = render(<WalletPanel wallet={wallet} mode="paper" />);

    const localBadge = screen.getByText('Local ledger').closest('span[data-tone]');
    expect(localBadge).toHaveAttribute('data-tone', 'info');
    expect(localBadge?.querySelector('svg')).not.toBeNull();

    const paperBadge = screen.getByText('Paper mode').closest('span[data-tone]');
    expect(paperBadge).toHaveAttribute('data-tone', 'info');
    expect(paperBadge?.querySelector('svg')).not.toBeNull();
    unmount();

    render(<WalletPanel wallet={{ ...wallet, source: 'venue' }} mode="live" />);

    const venueBadge = screen.getByText('Venue account').closest('span[data-tone]');
    expect(venueBadge).toHaveAttribute('data-tone', 'warn');
    expect(venueBadge?.querySelector('svg')).not.toBeNull();

    const liveBadge = screen.getByText('Real mode').closest('span[data-tone]');
    expect(liveBadge).toHaveAttribute('data-tone', 'warn');
    expect(liveBadge?.querySelector('svg')).not.toBeNull();
  });

  it('says that a live wallet mirrors the venue and is never debited locally', () => {
    const { unmount } = render(
      <WalletPanel wallet={{ ...wallet, source: 'venue' }} mode="live" />,
    );

    expect(screen.getByText(/mirrors the balance of the venue account/i)).toBeInTheDocument();
    expect(screen.getByText(/never debits it locally/i)).toBeInTheDocument();
    unmount();

    // A paper wallet funds orders from the local ledger: no venue claim at all.
    render(<WalletPanel wallet={wallet} mode="paper" />);
    expect(screen.queryByText(/never debits it locally/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/venue account/i)).not.toBeInTheDocument();
  });

  it('repeats the eight detailed values in an accessible table INSIDE the disclosure', () => {
    render(<WalletPanel wallet={wallet} mode="paper" />);

    const table = valuesTable();
    // The caption is the accessible name of the table: the text fallback is in
    // the accessibility tree, not only in the tiles...
    expect(table.querySelector('caption')).toHaveTextContent(/platform wallet/i);
    expect(within(table).getByRole('columnheader', { name: 'Wallet value' })).toBeInTheDocument();
    expect(within(table).getByRole('columnheader', { name: 'Amount' })).toBeInTheDocument();

    // One header row plus one row per detailed value.
    expect(within(table).getAllByRole('row')).toHaveLength(DETAIL_LABELS.length + 1);

    // ...and it is folded away with them, not left floating outside.
    expect(disclosure()).toContainElement(table);
    expect(table).not.toBeVisible();

    showDetails();

    expect(disclosure()).toContainElement(valuesTable());
    expect(valuesTable()).toBeVisible();

    for (const [label, value] of EXPECTED_DETAILS) {
      const row = within(valuesTable()).getByText(label).closest('tr');
      expect(row).toHaveTextContent(label);
      expect(row).toHaveTextContent(value);
    }
  });

  it('renders the em dash for every absent value and never NaN or undefined', () => {
    render(
      <WalletPanel
        mode="paper"
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
          total_cash: null,
          positions_value: null,
          total_portfolio_value: null,
        }}
      />,
    );

    // An absent total is the em dash placeholder: never `NaN`, never `undefined`.
    expect(within(tiles()).getAllByText(EMPTY_PLACEHOLDER)).toHaveLength(TOTAL_LABELS.length);
    expect(within(tiles()).queryByText(/NaN|undefined/)).not.toBeInTheDocument();

    showDetails();

    const grid = screen.getByTestId('wallet-details');
    expect(within(grid).getAllByText(EMPTY_PLACEHOLDER)).toHaveLength(DETAIL_LABELS.length);
    expect(screen.getByText('Wallet').nextElementSibling).toHaveTextContent(EMPTY_PLACEHOLDER);
    expect(screen.getByText('Updated').nextElementSibling).toHaveTextContent(EMPTY_PLACEHOLDER);

    // An absent P&L carries no trend claim: neither a glyph nor a label.
    expect(tile(screen.getByTestId('wallet-details'), 'Realized P&L')).not.toHaveTextContent('Flat');
    expect(tile(screen.getByTestId('wallet-details'), 'Unrealized P&L')).not.toHaveTextContent('Flat');

    const text = document.body.textContent ?? '';
    expect(text).not.toContain('NaN');
    expect(text).not.toContain('undefined');
  });

  it('renders the em dash state and a per-mode note when a mode holds no ledger row', () => {
    render(<WalletPanel wallet={null} mode="paper" />);

    // Never a blank panel, never a thrown error — and never a claim that the
    // whole platform has no wallet: the note is scoped to the selected mode.
    const note = screen.getByText('No paper trading ledger reported').closest('p');
    expect(note).toHaveTextContent(/no wallet snapshot for paper trading/i);
    expect(note).toHaveTextContent(/until that mode holds a ledger row/i);
    expect(within(tiles()).getAllByText(EMPTY_PLACEHOLDER)).toHaveLength(TOTAL_LABELS.length);
    expect(screen.queryByText(/the platform has no wallet/i)).not.toBeInTheDocument();

    // The mode badge still names the mode: it is driven by the prop, not by the
    // absent ledger. Only the source badge falls back to the neutral em dash.
    expect(screen.getByText('Paper mode')).toBeInTheDocument();
    const badges = document.querySelectorAll<HTMLElement>('span[data-tone]');
    expect(badges).toHaveLength(2);
    expect(badges[0]).toHaveAttribute('data-tone', 'neutral');
    expect(badges[0]).toHaveTextContent(EMPTY_PLACEHOLDER);
    expect(badges[0].querySelector('svg')).not.toBeNull();
    expect(badges[1]).toHaveAttribute('data-tone', 'info');
    expect(badges[1]).toHaveTextContent('Paper mode');

    // No source is claimed, and no live-mode sentence is shown.
    expect(screen.queryByText('Local ledger')).not.toBeInTheDocument();
    expect(screen.queryByText('Venue account')).not.toBeInTheDocument();
    expect(screen.queryByText(/never debits it locally/i)).not.toBeInTheDocument();

    // The eight detailed values and their table survive in that state too.
    showDetails();
    expect(valuesTable()).toBeInTheDocument();
    expect(within(valuesTable()).getAllByRole('row')).toHaveLength(DETAIL_LABELS.length + 1);
  });

  it('scopes the no-ledger note to the real mode when the real ledger is the missing one', () => {
    render(<WalletPanel wallet={null} mode="live" />);

    expect(screen.getByText('No real trading ledger reported')).toBeInTheDocument();
    // The mode badge still follows the prop, so the operator knows which side is
    // empty even when that side has no row at all.
    expect(screen.getByText('Real mode')).toBeInTheDocument();
    expect(
      screen.getByRole('heading', { level: 2, name: 'Platform wallet — real trading' }),
    ).toBeInTheDocument();
  });

  it('collapses the disclosure on the first render of every case, ledger or not', () => {
    const { unmount } = render(<WalletPanel wallet={wallet} mode="paper" />);
    expect(disclosure().open).toBe(false);
    unmount();

    render(<WalletPanel wallet={null} mode="live" />);
    expect(disclosure().open).toBe(false);
  });

  it('uses no named max-w-* / min-w-* utility anywhere, which density tokens would collapse', () => {
    const { container } = render(<WalletPanel wallet={wallet} mode="paper" />);

    // `--spacing-*` shadows the container scale in Tailwind v4 (pinned by
    // `src/app/globals.test.ts`): a named width class would collapse the panel.
    expect(container.innerHTML).not.toMatch(/\b(?:max-w|min-w)-[a-z]/);
  });
});
