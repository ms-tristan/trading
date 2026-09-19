import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { BUTTON_PRESSED_CLASSES } from '@/components/ui/button';
import { OPERATOR_TOKEN_STORAGE_KEY } from '@/lib/operator-token';

import {
  OperatorTokenForm,
  TOKEN_MISSING_MESSAGE,
  TOKEN_SAVED_MESSAGE,
} from './operator-token-form';

const TOKEN = 'super-secret-token';

/** Save `token` through the form of the current render. */
async function saveToken(user: ReturnType<typeof userEvent.setup>, token: string): Promise<void> {
  await user.type(screen.getByLabelText(/operator token/i), token);
  await user.click(screen.getByRole('button', { name: 'Save token' }));
}

beforeEach(() => {
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.sessionStorage.clear();
  window.localStorage.clear();
});

describe('OperatorTokenForm rendering', () => {
  it('renders the token field, the save button and the not-saved status first', () => {
    render(<OperatorTokenForm />);

    const input = screen.getByLabelText('Operator token');
    expect(input).toHaveAttribute('type', 'password');
    expect(input).toHaveAttribute('autocomplete', 'off');
    expect(input).toHaveAttribute('spellcheck', 'false');
    expect(input).toHaveAttribute('placeholder', 'Paste the token, then save');

    expect(screen.getByRole('button', { name: 'Save token' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Clear token' })).not.toBeInTheDocument();

    const status = screen.getByRole('status');
    expect(status).toHaveAttribute('aria-live', 'polite');
    expect(status).toHaveTextContent(TOKEN_MISSING_MESSAGE);
  });

  it('carries the pressed state of its buttons and an accessible name that never changes', () => {
    render(<OperatorTokenForm />);

    expect(screen.getByRole('button', { name: 'Save token' })).toHaveClass(
      BUTTON_PRESSED_CLASSES.secondary,
    );
    expect(screen.getByLabelText('Operator token')).toBeInTheDocument();
  });

  it('renders its own label when one is passed', () => {
    render(<OperatorTokenForm label="Operator token of this page" />);

    expect(screen.getByLabelText('Operator token of this page')).toBeInTheDocument();
  });

  it('renders the optional hint only when it is passed', () => {
    const { unmount } = render(<OperatorTokenForm hint="Save it before submitting." />);
    expect(screen.getByText('Save it before submitting.')).toBeInTheDocument();
    unmount();

    render(<OperatorTokenForm />);
    expect(screen.queryByText('Save it before submitting.')).not.toBeInTheDocument();
  });
});

describe('OperatorTokenForm saving', () => {
  it('stores the token in sessionStorage only and never renders it back', async () => {
    const user = userEvent.setup();
    render(<OperatorTokenForm />);

    const input = screen.getByLabelText(/operator token/i);
    await saveToken(user, TOKEN);

    expect(window.sessionStorage.getItem(OPERATOR_TOKEN_STORAGE_KEY)).toBe(TOKEN);
    // The storage contract is frozen: no localStorage entry of any kind.
    expect(window.localStorage.getItem(OPERATOR_TOKEN_STORAGE_KEY)).toBeNull();
    expect(window.localStorage.length).toBe(0);

    // The secret leaves the DOM as soon as it is stored.
    expect(input).toHaveValue('');
    expect(document.body.textContent ?? '').not.toContain(TOKEN);
    expect(document.body.innerHTML).not.toContain(TOKEN);
  });

  it('reports that a token is stored and only then offers to clear it', async () => {
    const user = userEvent.setup();
    render(<OperatorTokenForm />);

    await saveToken(user, TOKEN);

    expect(screen.getByText(/token saved for this tab/i)).toHaveTextContent(TOKEN_SAVED_MESSAGE);
    expect(screen.getByRole('button', { name: 'Clear token' })).toBeInTheDocument();
  });

  it('removes the key instead of storing a blank secret', async () => {
    const user = userEvent.setup();
    window.sessionStorage.setItem(OPERATOR_TOKEN_STORAGE_KEY, TOKEN);
    render(<OperatorTokenForm />);

    await user.type(screen.getByLabelText(/operator token/i), '   ');
    await user.click(screen.getByRole('button', { name: 'Save token' }));

    expect(window.sessionStorage.getItem(OPERATOR_TOKEN_STORAGE_KEY)).toBeNull();
    expect(screen.getByText(/no token saved in this tab/i)).toBeInTheDocument();
  });

  it('forgets the token on request', async () => {
    const user = userEvent.setup();
    render(<OperatorTokenForm />);

    await saveToken(user, TOKEN);
    await user.click(screen.getByRole('button', { name: 'Clear token' }));

    expect(window.sessionStorage.getItem(OPERATOR_TOKEN_STORAGE_KEY)).toBeNull();
    expect(screen.getByText(/no token saved in this tab/i)).toHaveTextContent(TOKEN_MISSING_MESSAGE);
    expect(screen.queryByRole('button', { name: 'Clear token' })).not.toBeInTheDocument();
  });

  it('starts from the stored state of the tab', () => {
    window.sessionStorage.setItem(OPERATOR_TOKEN_STORAGE_KEY, TOKEN);
    render(<OperatorTokenForm />);

    expect(screen.getByText(/token saved for this tab/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Clear token' })).toBeInTheDocument();
  });

  it('never leaks the token into a logged or thrown value', async () => {
    const user = userEvent.setup();
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    render(<OperatorTokenForm />);

    await saveToken(user, TOKEN);

    expect(errorSpy).not.toHaveBeenCalled();
    expect(screen.getByRole('status')).not.toHaveTextContent(TOKEN);
  });
});

describe('OperatorTokenForm without a usable storage', () => {
  it('keeps working when sessionStorage throws', async () => {
    const user = userEvent.setup();
    const throwing = {
      getItem: () => {
        throw new Error('storage is disabled');
      },
      setItem: () => {
        throw new Error('storage is disabled');
      },
      removeItem: () => {
        throw new Error('storage is disabled');
      },
    } as unknown as Storage;
    const accessor = vi.spyOn(window, 'sessionStorage', 'get').mockReturnValue(throwing);
    render(<OperatorTokenForm />);

    expect(screen.getByRole('status')).toHaveTextContent(TOKEN_MISSING_MESSAGE);

    await saveToken(user, TOKEN);

    // No crash, no bogus "saved" claim: the form survives a dead storage.
    expect(screen.getByRole('status')).toHaveTextContent(TOKEN_MISSING_MESSAGE);
    expect(screen.getByLabelText(/operator token/i)).toHaveValue('');
    expect(accessor).toHaveBeenCalled();

    accessor.mockRestore();
  });

  it('keeps working when the storage accessor itself throws', async () => {
    const user = userEvent.setup();
    vi.spyOn(window, 'sessionStorage', 'get').mockImplementation(() => {
      throw new Error('storage is disabled');
    });
    render(<OperatorTokenForm />);

    await saveToken(user, TOKEN);

    expect(screen.getByRole('status')).toHaveTextContent(TOKEN_MISSING_MESSAGE);
  });
});
