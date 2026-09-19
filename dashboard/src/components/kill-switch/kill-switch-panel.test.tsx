import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { BUTTON_PRESSED_CLASSES } from '@/components/ui/button';
import { OPERATOR_TOKEN_STORAGE_KEY } from '@/lib/operator-token';
import type { KillSwitchPayload } from '@/lib/types';

import { KillSwitchPanel } from './kill-switch-panel';

const released: KillSwitchPayload = { kill_switch: false, reason: '', changed_at: null };

const engaged: KillSwitchPayload = {
  kill_switch: true,
  reason: 'drawdown breach',
  changed_at: '2024-01-01T00:00:00+00:00',
};

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

function installFetch(response: Response | Error): RecordedCall[] {
  const calls: RecordedCall[] = [];
  const impl = async (url: unknown, init?: RequestInit): Promise<Response> => {
    calls.push({ url: String(url), init });
    if (response instanceof Error) {
      throw response;
    }
    return response;
  };
  vi.stubGlobal('fetch', impl as unknown as typeof fetch);
  return calls;
}

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

describe('KillSwitchPanel state', () => {
  it('shows the released state with a text label and an icon', () => {
    const { container } = render(<KillSwitchPanel initialState={released} />);

    const label = screen.getByText('Released');
    expect(label).toBeInTheDocument();
    expect(label.closest('[data-state="released"]')).not.toBeNull();
    expect(container.querySelector('[data-state="released"] svg')).not.toBeNull();
  });

  it('shows the engaged state with its reason and timestamp', () => {
    const { container } = render(<KillSwitchPanel initialState={engaged} />);

    expect(screen.getByText('Engaged')).toBeInTheDocument();
    expect(container.querySelector('[data-state="engaged"] svg')).not.toBeNull();
    expect(screen.getByText('drawdown breach')).toBeInTheDocument();
    expect(screen.getByText('2024-01-01 00:00:00 UTC')).toBeInTheDocument();
  });

  it('prefers the live state over the rendered initial state', () => {
    const { rerender } = render(<KillSwitchPanel initialState={released} />);
    expect(screen.getByText('Released')).toBeInTheDocument();

    rerender(<KillSwitchPanel initialState={released} state={engaged} />);
    expect(screen.getByText('Engaged')).toBeInTheDocument();
    expect(screen.queryByText('Released')).not.toBeInTheDocument();
  });

  it('disables the action that matches the current state', () => {
    const releasedPanel = render(<KillSwitchPanel initialState={released} />);
    expect(screen.getByRole('button', { name: 'Engage kill switch' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Release kill switch' })).toBeDisabled();
    releasedPanel.unmount();

    render(<KillSwitchPanel initialState={engaged} />);
    expect(screen.getByRole('button', { name: 'Engage kill switch' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Release kill switch' })).toBeEnabled();
  });
});

describe('KillSwitchPanel operator token', () => {
  it('stores the token in sessionStorage, clears the field and never renders it back', async () => {
    const user = userEvent.setup();
    render(<KillSwitchPanel initialState={released} />);

    const input = screen.getByLabelText(/operator token/i);
    expect(input).toHaveAttribute('type', 'password');
    expect(input).toHaveAttribute('autocomplete', 'off');

    await saveToken(user, 'super-secret-token');

    expect(window.sessionStorage.getItem(OPERATOR_TOKEN_STORAGE_KEY)).toBe('super-secret-token');
    expect(window.localStorage.getItem(OPERATOR_TOKEN_STORAGE_KEY)).toBeNull();
    expect(window.localStorage.length).toBe(0);
    expect(input).toHaveValue('');
    expect(document.body.textContent ?? '').not.toContain('super-secret-token');
    expect(screen.getByText(/token saved for this tab/i)).toBeInTheDocument();
  });

  it('forgets the token on request', async () => {
    const user = userEvent.setup();
    render(<KillSwitchPanel initialState={released} />);

    await saveToken(user, 'super-secret-token');
    await user.click(screen.getByRole('button', { name: 'Clear token' }));

    expect(window.sessionStorage.getItem(OPERATOR_TOKEN_STORAGE_KEY)).toBeNull();
    expect(screen.getByText(/no token saved in this tab/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Clear token' })).not.toBeInTheDocument();
  });

  it('reports no token before anything is saved', () => {
    render(<KillSwitchPanel initialState={released} />);
    expect(screen.getByText(/no token saved in this tab/i)).toBeInTheDocument();
  });
});

describe('KillSwitchPanel confirmation flow', () => {
  it('requires an explicit confirmation before engaging', async () => {
    const user = userEvent.setup();
    const calls = installFetch(jsonResponse(engaged));
    render(<KillSwitchPanel initialState={released} />);

    await user.click(screen.getByRole('button', { name: 'Engage kill switch' }));

    const dialog = screen.getByRole('dialog', { name: /engage the kill switch/i });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(calls).toHaveLength(0);

    // Focus moved into the dialog, on the reason field.
    expect(within(dialog).getByLabelText(/reason/i)).toHaveFocus();

    // The reason is mandatory: the confirm button stays disabled while it is empty.
    expect(within(dialog).getByRole('button', { name: 'Engage kill switch' })).toBeDisabled();
  });

  it('cancels without posting and returns the focus to the trigger', async () => {
    const user = userEvent.setup();
    const calls = installFetch(jsonResponse(engaged));
    render(<KillSwitchPanel initialState={released} />);

    const trigger = screen.getByRole('button', { name: 'Engage kill switch' });
    await user.click(trigger);
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }));

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(calls).toHaveLength(0);
    expect(trigger).toHaveFocus();
  });

  it('cancels on Escape', async () => {
    const user = userEvent.setup();
    const calls = installFetch(jsonResponse(engaged));
    render(<KillSwitchPanel initialState={released} />);

    await user.click(screen.getByRole('button', { name: 'Engage kill switch' }));
    expect(screen.getByRole('dialog')).toBeInTheDocument();

    await user.keyboard('{Escape}');

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(calls).toHaveLength(0);
  });

  it('posts the engagement with the reason and the operator token', async () => {
    const user = userEvent.setup();
    const calls = installFetch(jsonResponse(engaged));
    render(<KillSwitchPanel initialState={released} />);

    await saveToken(user, 'operator-secret');
    await user.click(screen.getByRole('button', { name: 'Engage kill switch' }));

    const dialog = screen.getByRole('dialog');
    await user.type(within(dialog).getByLabelText(/reason/i), 'manual stop');
    await user.click(within(dialog).getByRole('button', { name: 'Engage kill switch' }));

    await waitFor(() => {
      expect(calls).toHaveLength(1);
    });

    expect(calls[0]?.url).toBe('/api/kill-switch');
    expect(calls[0]?.init?.method).toBe('POST');
    expect(calls[0]?.init?.body).toBe(JSON.stringify({ engage: true, reason: 'manual stop' }));
    const headers = calls[0]?.init?.headers as Record<string, string>;
    expect(headers['Content-Type']).toBe('application/json');
    expect(headers['X-Operator-Token']).toBe('operator-secret');

    // The response replaces the displayed state.
    await waitFor(() => {
      expect(screen.getByText('Engaged')).toBeInTheDocument();
    });
    expect(screen.getByText('drawdown breach')).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('sends an empty token when the operator saved none (the server refuses it)', async () => {
    const user = userEvent.setup();
    const calls = installFetch(jsonResponse(engaged));
    render(<KillSwitchPanel initialState={released} />);

    await user.click(screen.getByRole('button', { name: 'Engage kill switch' }));
    const dialog = screen.getByRole('dialog');
    await user.type(within(dialog).getByLabelText(/reason/i), 'manual stop');
    await user.click(within(dialog).getByRole('button', { name: 'Engage kill switch' }));

    await waitFor(() => {
      expect(calls).toHaveLength(1);
    });
    const headers = calls[0]?.init?.headers as Record<string, string>;
    expect(headers['X-Operator-Token']).toBe('');
  });

  it('confirms a release as well', async () => {
    const user = userEvent.setup();
    const calls = installFetch(jsonResponse(released));
    render(<KillSwitchPanel initialState={engaged} />);

    await user.click(screen.getByRole('button', { name: 'Release kill switch' }));

    const dialog = screen.getByRole('dialog', { name: /release the kill switch/i });
    await user.click(within(dialog).getByRole('button', { name: 'Release kill switch' }));

    await waitFor(() => {
      expect(calls).toHaveLength(1);
    });
    expect(calls[0]?.init?.body).toBe(JSON.stringify({ engage: false, reason: '' }));

    await waitFor(() => {
      expect(screen.getByText('Released')).toBeInTheDocument();
    });
  });
});

describe('KillSwitchPanel failures', () => {
  it('shows the read-only refusal of the server verbatim and keeps the state', async () => {
    const user = userEvent.setup();
    installFetch(jsonResponse({ error: 'mutations are disabled on this server' }, 403));
    render(<KillSwitchPanel initialState={released} />);

    await user.click(screen.getByRole('button', { name: 'Engage kill switch' }));
    const dialog = screen.getByRole('dialog');
    await user.type(within(dialog).getByLabelText(/reason/i), 'manual stop');
    await user.click(within(dialog).getByRole('button', { name: 'Engage kill switch' }));

    await waitFor(() => {
      expect(screen.getByText('The monitoring API refused the request')).toBeInTheDocument();
    });
    // The documented 403 contract survives: the server text stays verbatim, with
    // its status, as the detail of the mapped headline.
    expect(screen.getByTestId('error-banner-detail')).toHaveTextContent(
      'HTTP 403 · mutations are disabled on this server',
    );
    expect(screen.getByText('Released')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Engage kill switch' })).toBeEnabled();
  });

  it('shows the invalid-token refusal verbatim', async () => {
    const user = userEvent.setup();
    installFetch(jsonResponse({ error: 'missing or invalid operator token' }, 403));
    render(<KillSwitchPanel initialState={released} />);

    await user.click(screen.getByRole('button', { name: 'Engage kill switch' }));
    const dialog = screen.getByRole('dialog');
    await user.type(within(dialog).getByLabelText(/reason/i), 'manual stop');
    await user.click(within(dialog).getByRole('button', { name: 'Engage kill switch' }));

    await waitFor(() => {
      expect(screen.getByText('The monitoring API refused the request')).toBeInTheDocument();
    });
    expect(screen.getByTestId('error-banner-detail')).toHaveTextContent(
      'HTTP 403 · missing or invalid operator token',
    );
    expect(screen.getByText('Released')).toBeInTheDocument();
  });

  it('shows a non-blocking banner on a network failure and keeps the previous state', async () => {
    const user = userEvent.setup();
    installFetch(new TypeError('fetch failed'));
    render(<KillSwitchPanel initialState={released} />);

    await user.click(screen.getByRole('button', { name: 'Engage kill switch' }));
    const dialog = screen.getByRole('dialog');
    await user.type(within(dialog).getByLabelText(/reason/i), 'manual stop');
    await user.click(within(dialog).getByRole('button', { name: 'Engage kill switch' }));

    await waitFor(() => {
      expect(screen.getByText('The monitoring API is unreachable')).toBeInTheDocument();
    });
    expect(screen.getByTestId('error-banner-detail')).toHaveTextContent(
      'network error calling /api/kill-switch',
    );
    expect(screen.getByText('Released')).toBeInTheDocument();
  });

  it('reads a proxy 502 as the monitoring API being unreachable, never as a bare status', async () => {
    const user = userEvent.setup();
    installFetch(jsonResponse({ error: 'Bad Gateway' }, 502));
    render(<KillSwitchPanel initialState={released} />);

    await user.click(screen.getByRole('button', { name: 'Engage kill switch' }));
    const dialog = screen.getByRole('dialog');
    await user.type(within(dialog).getByLabelText(/reason/i), 'manual stop');
    await user.click(within(dialog).getByRole('button', { name: 'Engage kill switch' }));

    await waitFor(() => {
      expect(screen.getByText('The monitoring API is unreachable')).toBeInTheDocument();
    });
    const detail = screen.getByTestId('error-banner-detail');
    expect(detail).toHaveTextContent('HTTP 502');
    expect(detail).toHaveTextContent('Bad Gateway');
    expect(screen.queryByText('HTTP 502')).not.toBeInTheDocument();
    // The previous state stays on screen and the control stays usable.
    expect(screen.getByText('Released')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Engage kill switch' })).toBeEnabled();
  });

  it('never leaks the operator token into the displayed error', async () => {
    const user = userEvent.setup();
    installFetch(new Error('connection refused for token operator-secret'));
    render(<KillSwitchPanel initialState={released} />);

    await saveToken(user, 'operator-secret');
    await user.click(screen.getByRole('button', { name: 'Engage kill switch' }));
    const dialog = screen.getByRole('dialog');
    await user.type(within(dialog).getByLabelText(/reason/i), 'manual stop');
    await user.click(within(dialog).getByRole('button', { name: 'Engage kill switch' }));

    await waitFor(() => {
      expect(screen.getByText('The monitoring API is unreachable')).toBeInTheDocument();
    });
    // The mapping only ever receives the already-scrubbed ApiError.
    expect(screen.getByTestId('error-banner-detail')).toHaveTextContent(
      'network error calling /api/kill-switch: connection refused for token ***',
    );
    expect(document.body.textContent ?? '').not.toContain('operator-secret');
    expect(document.body.innerHTML).not.toContain('operator-secret');
  });
});

describe('KillSwitchPanel press feedback', () => {
  it('gives every control of the panel its pressed state', async () => {
    const user = userEvent.setup();
    installFetch(jsonResponse(engaged));
    const view = render(<KillSwitchPanel initialState={released} />);

    // A control that can be pressed carries the pressed state of its variant.
    expect(screen.getByRole('button', { name: 'Engage kill switch' })).toHaveClass(
      BUTTON_PRESSED_CLASSES.danger,
    );
    expect(screen.getByRole('button', { name: 'Save token' })).toHaveClass(
      BUTTON_PRESSED_CLASSES.secondary,
    );

    // The confirmation dialog follows the shared Button of the design system too.
    await user.click(screen.getByRole('button', { name: 'Engage kill switch' }));
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toHaveClass(
      BUTTON_PRESSED_CLASSES.ghost,
    );
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    // Release is only offered while the kill switch is engaged.
    view.rerender(<KillSwitchPanel initialState={released} state={engaged} />);
    expect(screen.getByRole('button', { name: 'Release kill switch' })).toHaveClass(
      BUTTON_PRESSED_CLASSES.secondary,
    );
  });

  it('strips the pressed state from a control locked by a pending mutation', async () => {
    const user = userEvent.setup();
    // A request that never answers: the panel stays pending for the whole test.
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise<Response>(() => {})),
    );
    render(<KillSwitchPanel initialState={released} />);

    await user.click(screen.getByRole('button', { name: 'Engage kill switch' }));
    const dialog = screen.getByRole('dialog');
    await user.type(within(dialog).getByLabelText(/reason/i), 'manual stop');
    await user.click(within(dialog).getByRole('button', { name: 'Engage kill switch' }));

    // The panel announces the in-flight mutation...
    const panel = screen.getByRole('heading', { name: 'Kill switch' }).closest('section');
    expect(panel).toHaveAttribute('aria-busy', 'true');

    // ...and every locked control loses its press feedback.
    const release = screen.getByRole('button', { name: 'Release kill switch' });
    expect(release).toBeDisabled();
    expect(release).not.toHaveClass(BUTTON_PRESSED_CLASSES.secondary);
    expect(Array.from(release.classList).some((name) => name.includes('active:'))).toBe(false);

    const engage = screen.getByRole('button', { name: 'Engage kill switch' });
    expect(engage).toBeDisabled();
    expect(engage).not.toHaveClass(BUTTON_PRESSED_CLASSES.danger);

    // A control that stays usable keeps its press feedback.
    expect(screen.getByRole('button', { name: 'Save token' })).toHaveClass(
      BUTTON_PRESSED_CLASSES.secondary,
    );
  });
});
