import { useState } from 'react';

import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  DEFAULT_EMPTY_MESSAGE,
  DEFAULT_MAX_VISIBLE_OPTIONS,
  ProfileCombobox,
  filterComboboxOptions,
  type ProfileComboboxOption,
} from './profile-combobox';

const SYMBOLS: ProfileComboboxOption[] = [
  { value: 'BTC/USDT', label: 'BTC/USDT', hint: 'BTC / USDT' },
  { value: 'ETH/USDT', label: 'ETH/USDT', hint: 'ETH / USDT' },
  { value: 'SOL/USDT', label: 'SOL/USDT', hint: 'SOL / USDT' },
];

function manyOptions(count: number): ProfileComboboxOption[] {
  return Array.from({ length: count }, (_unused, index) => ({
    value: `SYM${String(index)}/USDT`,
    label: `SYM${String(index)}/USDT`,
  }));
}

interface HarnessProps {
  options?: ProfileComboboxOption[];
  initialValue?: string;
  maxVisibleOptions?: number;
  disabled?: boolean;
  error?: string | null;
  emptyMessage?: string;
  onChange?: (value: string) => void;
}

/**
 * Controlled harness: the component owns no selection state, so the harness owns
 * it exactly like the creation form does.
 */
function Harness({ options = SYMBOLS, initialValue = '', onChange, ...rest }: HarnessProps) {
  const [value, setValue] = useState<string>(initialValue);
  return (
    <>
      <ProfileCombobox
        id="asset"
        label="Asset"
        options={options}
        value={value}
        onChange={(next) => {
          setValue(next);
          onChange?.(next);
        }}
        {...rest}
      />
      <button type="button">After</button>
    </>
  );
}

function input(): HTMLInputElement {
  return screen.getByRole('combobox') as HTMLInputElement;
}

function listbox(): HTMLElement {
  return screen.getByRole('listbox');
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('filterComboboxOptions', () => {
  it('filters on the value and the label, case-insensitively', () => {
    expect(filterComboboxOptions(SYMBOLS, 'btc').map((option) => option.value)).toEqual([
      'BTC/USDT',
    ]);
    expect(filterComboboxOptions(SYMBOLS, 'USDT')).toHaveLength(3);
    expect(filterComboboxOptions(SYMBOLS, '   ')).toHaveLength(3);
    expect(filterComboboxOptions(SYMBOLS, 'nope')).toHaveLength(0);
  });
});

describe('ProfileCombobox accessibility contract', () => {
  it('exposes the combobox pattern and the listbox of options', async () => {
    const user = userEvent.setup();
    render(<Harness />);

    const field = input();
    expect(field).toHaveAttribute('role', 'combobox');
    expect(field).toHaveAttribute('aria-expanded', 'false');
    expect(field).toHaveAttribute('aria-controls', 'asset-listbox');
    expect(field).toHaveAttribute('aria-autocomplete', 'list');
    expect(field).toHaveAttribute('autocomplete', 'off');
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();

    await user.click(field);

    expect(field).toHaveAttribute('aria-expanded', 'true');
    const box = listbox();
    expect(box).toHaveAttribute('id', 'asset-listbox');
    expect(box).toHaveAttribute('aria-label', 'Asset');
    expect(within(box).getAllByRole('option')).toHaveLength(3);
    expect(screen.getByRole('option', { name: /BTC \/ USDT/ })).toBeInTheDocument();
  });

  it('shows the selected label while the list is closed', () => {
    render(<Harness initialValue="ETH/USDT" />);

    expect(input()).toHaveValue('ETH/USDT');
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  });

  it('renders the error next to the field and links it with aria-describedby', () => {
    render(<Harness error="Asset is required." />);

    const field = input();
    expect(field).toHaveAttribute('aria-invalid', 'true');
    expect(field).toHaveAttribute('aria-describedby', 'asset-error');
    expect(document.getElementById('asset-error')).toHaveTextContent('Asset is required.');
  });
});

describe('ProfileCombobox filtering', () => {
  it('filters the options while typing, case-insensitively on substrings', async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(input());
    await user.type(input(), 'et');

    expect(input()).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getAllByRole('option')).toHaveLength(1);
    expect(screen.getByRole('option', { name: /ETH\/USDT/ })).toBeInTheDocument();
  });

  it('caps the rendered rows and reports how many matches exist', async () => {
    const user = userEvent.setup();
    render(<Harness options={manyOptions(500)} />);

    await user.click(input());

    expect(screen.getAllByRole('option')).toHaveLength(DEFAULT_MAX_VISIBLE_OPTIONS);
    expect(screen.getByText('50 of 500 matches')).toBeInTheDocument();

    await user.type(input(), 'SYM42/');

    expect(screen.getAllByRole('option')).toHaveLength(1);
    expect(screen.getByText('1 of 1 matches')).toBeInTheDocument();
  });

  it('honours a custom visible window', async () => {
    const user = userEvent.setup();
    render(<Harness options={manyOptions(20)} maxVisibleOptions={5} />);

    await user.click(input());

    expect(screen.getAllByRole('option')).toHaveLength(5);
    expect(screen.getByText('5 of 20 matches')).toBeInTheDocument();
  });

  it('renders the explicit empty state instead of a blank listbox', async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(input());
    await user.type(input(), 'zzz');

    expect(input()).toHaveAttribute('aria-expanded', 'true');
    expect(screen.queryAllByRole('option')).toHaveLength(0);
    expect(within(listbox()).getByText(DEFAULT_EMPTY_MESSAGE)).toBeInTheDocument();
  });

  it('uses a caller-supplied empty message', async () => {
    const user = userEvent.setup();
    render(<Harness emptyMessage="No strategy matches." />);

    await user.click(input());
    await user.type(input(), 'zzz');

    expect(within(listbox()).getByText('No strategy matches.')).toBeInTheDocument();
  });
});

describe('ProfileCombobox keyboard', () => {
  it('opens the list on ArrowDown and moves the active option with the arrows', async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await user.tab();
    expect(input()).toHaveFocus();
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();

    await user.keyboard('{ArrowDown}');
    expect(screen.getByRole('listbox')).toBeInTheDocument();
    expect(input()).toHaveAttribute('aria-activedescendant', 'asset-option-0');
    // The keyboard never moves the DOM focus out of the field.
    expect(input()).toHaveFocus();

    await user.keyboard('{ArrowDown}');
    expect(input()).toHaveAttribute('aria-activedescendant', 'asset-option-1');

    await user.keyboard('{ArrowDown}');
    expect(input()).toHaveAttribute('aria-activedescendant', 'asset-option-2');

    // Wrapping forward, then backward.
    await user.keyboard('{ArrowDown}');
    expect(input()).toHaveAttribute('aria-activedescendant', 'asset-option-0');

    await user.keyboard('{ArrowUp}');
    expect(input()).toHaveAttribute('aria-activedescendant', 'asset-option-2');

    await user.keyboard('{Home}');
    expect(input()).toHaveAttribute('aria-activedescendant', 'asset-option-0');

    await user.keyboard('{End}');
    expect(input()).toHaveAttribute('aria-activedescendant', 'asset-option-2');
    expect(input()).toHaveFocus();
  });

  it('opens on ArrowUp at the last match and selects it with Enter', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);

    await user.tab();
    await user.keyboard('{ArrowUp}');
    expect(input()).toHaveAttribute('aria-activedescendant', 'asset-option-2');

    await user.keyboard('{Enter}');

    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith('SOL/USDT');
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(input()).toHaveValue('SOL/USDT');
  });

  it('activates the first match while typing and selects it with Enter', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);

    await user.click(input());
    await user.type(input(), 'eth');
    expect(input()).toHaveAttribute('aria-activedescendant', 'asset-option-0');

    await user.keyboard('{Enter}');

    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith('ETH/USDT');
  });

  it('closes on Escape, reverts the typed text and keeps the selection', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness initialValue="BTC/USDT" onChange={onChange} />);

    await user.click(input());
    await user.clear(input());
    await user.type(input(), 'eth');
    expect(input()).toHaveValue('eth');

    await user.keyboard('{Escape}');

    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(input()).toHaveAttribute('aria-expanded', 'false');
    expect(input()).toHaveValue('BTC/USDT');
    expect(onChange).not.toHaveBeenCalled();
  });

  it('closes on Tab without trapping the focus', async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(input());
    expect(screen.getByRole('listbox')).toBeInTheDocument();

    await user.tab();

    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'After' })).toHaveFocus();
  });

  it('re-filters when the operator types again after a selection', async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(input());
    await user.type(input(), 'eth');
    await user.keyboard('{Enter}');
    expect(input()).toHaveValue('ETH/USDT');
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();

    await user.clear(input());
    await user.type(input(), 'btc');

    expect(input()).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getAllByRole('option')).toHaveLength(1);
    expect(screen.getByRole('option', { name: /BTC\/USDT/ })).toBeInTheDocument();
  });
});

describe('ProfileCombobox pointer and lifecycle', () => {
  it('selects a clicked option and closes the list', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);

    await user.click(input());
    await user.click(screen.getByRole('option', { name: /SOL\/USDT/ }));

    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith('SOL/USDT');
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(input()).toHaveValue('SOL/USDT');
  });

  it('closes on blur without changing the selection', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness initialValue="BTC/USDT" onChange={onChange} />);

    await user.click(input());
    await user.clear(input());
    await user.type(input(), 'sol');
    await user.click(screen.getByRole('button', { name: 'After' }));

    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(input()).toHaveValue('BTC/USDT');
    expect(onChange).not.toHaveBeenCalled();
  });

  it('scrolls the active option into view', async () => {
    const user = userEvent.setup();
    const original = Element.prototype.scrollIntoView;
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    render(<Harness />);

    await user.click(input());
    await user.keyboard('{ArrowDown}');

    expect(scrollIntoView).toHaveBeenCalled();
    Element.prototype.scrollIntoView = original;
  });

  it('cannot be opened while disabled', async () => {
    const user = userEvent.setup();
    render(<Harness disabled />);

    const field = input();
    expect(field).toBeDisabled();

    await user.click(field);
    await user.keyboard('{ArrowDown}{Enter}');

    expect(field).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  });
});
