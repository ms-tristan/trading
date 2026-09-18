/**
 * Storage of the operator token used by `POST /api/kill-switch`.
 *
 * The token lives in `sessionStorage` only: never `localStorage` (it would
 * survive the tab), never a cookie (it would be sent with every request) and
 * never a URL. It is never rendered back to the screen, never logged and never
 * included in an error message — the UI only ever shows whether a token is
 * stored.
 *
 * Every access is guarded: the module is imported by Client Components, so it
 * must survive server-side evaluation (no `window`) and a storage that throws
 * (private mode, disabled storage, quota).
 */

/** Key under which the operator token is stored. */
export const OPERATOR_TOKEN_STORAGE_KEY = 'trading-monitor.operator-token';

/** Return the `sessionStorage` of the current window, or `null`. */
function storage(): Storage | null {
  try {
    if (typeof window === 'undefined') {
      return null;
    }
    return window.sessionStorage ?? null;
  } catch {
    // Accessing `window.sessionStorage` itself can throw in a hardened browser.
    return null;
  }
}

/** Return the stored token, or `null` when absent, empty or unreadable. */
export function readOperatorToken(): string | null {
  const store = storage();
  if (store === null) {
    return null;
  }
  try {
    const raw = store.getItem(OPERATOR_TOKEN_STORAGE_KEY);
    if (raw === null) {
      return null;
    }
    const trimmed = raw.trim();
    return trimmed === '' ? null : trimmed;
  } catch {
    return null;
  }
}

/**
 * Store `token`. An empty or whitespace-only value removes the key instead of
 * storing a blank secret.
 */
export function saveOperatorToken(token: string): void {
  const store = storage();
  if (store === null) {
    return;
  }
  const trimmed = token.trim();
  try {
    if (trimmed === '') {
      store.removeItem(OPERATOR_TOKEN_STORAGE_KEY);
      return;
    }
    store.setItem(OPERATOR_TOKEN_STORAGE_KEY, trimmed);
  } catch {
    // A storage failure must never break the operator action.
  }
}

/** Remove the stored token. */
export function clearOperatorToken(): void {
  const store = storage();
  if (store === null) {
    return;
  }
  try {
    store.removeItem(OPERATOR_TOKEN_STORAGE_KEY);
  } catch {
    // Ignored on purpose (see `saveOperatorToken`).
  }
}

/** Whether a usable token is currently stored. */
export function hasOperatorToken(): boolean {
  return readOperatorToken() !== null;
}
