'use client';

/**
 * Operator token form: the single save / clear surface of the operator token.
 *
 * It is rendered by every operator-facing page that needs a token — the
 * kill-switch panel of the overview and the profile creation form — so saving
 * the token is never reachable from one page only. Both surfaces show exactly
 * the same control and report the same two sentences, because they render this
 * very component.
 *
 * Security rules that this component implements:
 *
 * * the token lives in `sessionStorage` only — never `localStorage` (it would
 *   survive the tab), never a cookie (it would be sent with every request) and
 *   never a URL. The storage helpers of `@/lib/operator-token` enforce it;
 * * the value is submitted through a `type="password"` input whose value is
 *   cleared from the DOM immediately after saving, is never mirrored into React
 *   state, never rendered back, never logged and never placed in an error
 *   message. The status line only ever reports *whether* a token is stored;
 * * every storage access stays guarded: an unavailable, throwing or server-side
 *   `sessionStorage` is a no-op and the form keeps working.
 *
 * `sessionStorage` is a browser-only external store: it is read through
 * `useSyncExternalStore`, whose server snapshot is always `false`. That keeps
 * the status line free of a hydration mismatch and free of a `setState` inside
 * an effect. This component is the single owner of the subscription and of the
 * notification, so the saving surfaces that live outside it re-read the storage
 * themselves.
 */

import { useCallback, useId, useRef, useSyncExternalStore } from 'react';
import type { FormEvent, JSX } from 'react';

import { KeyRound, Trash2 } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { cn } from '@/lib/cn';
import { clearOperatorToken, hasOperatorToken, saveOperatorToken } from '@/lib/operator-token';

/** Status line shown while a token is stored in this tab. */
export const TOKEN_SAVED_MESSAGE =
  'Token saved for this tab (session storage only). It is never shown again.';

/** Status line shown while no token is stored in this tab. */
export const TOKEN_MISSING_MESSAGE = 'No token saved in this tab.';

/** Props of {@link OperatorTokenForm}. */
export interface OperatorTokenFormProps {
  /** Extra operator-facing hint rendered under the status line (optional). */
  hint?: string;
  /** Accessible, visible label of the section; defaults to 'Operator token'. */
  label?: string;
  className?: string;
}

const tokenListeners = new Set<() => void>();

function subscribeTokenSaved(listener: () => void): () => void {
  tokenListeners.add(listener);
  return () => {
    tokenListeners.delete(listener);
  };
}

function readTokenSavedSnapshot(): boolean {
  return hasOperatorToken();
}

function readTokenSavedServerSnapshot(): boolean {
  return false;
}

/** Tell every mounted form that the stored token changed. */
function notifyTokenSaved(): void {
  for (const listener of tokenListeners) {
    listener();
  }
}

/**
 * Save the operator token, or forget it.
 *
 * The same control is rendered on every surface that needs a token, so the
 * operator can set it, see whether it is stored and clear it from either page —
 * the storage contract itself is unchanged (`sessionStorage` only).
 */
export function OperatorTokenForm({
  hint,
  label = 'Operator token',
  className,
}: OperatorTokenFormProps): JSX.Element {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const tokenId = useId();

  const tokenSaved = useSyncExternalStore(
    subscribeTokenSaved,
    readTokenSavedSnapshot,
    readTokenSavedServerSnapshot,
  );

  const handleSubmit = useCallback((event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const input = inputRef.current;
    const value = input === null ? '' : input.value;
    saveOperatorToken(value);
    if (input !== null) {
      // The secret never stays in the DOM once it has been stored.
      input.value = '';
    }
    notifyTokenSaved();
  }, []);

  const handleClear = useCallback(() => {
    clearOperatorToken();
    const input = inputRef.current;
    if (input !== null) {
      input.value = '';
    }
    notifyTokenSaved();
  }, []);

  return (
    <form onSubmit={handleSubmit} className={cn('min-w-0', className)}>
      <label
        htmlFor={tokenId}
        className="block text-xs font-medium uppercase tracking-wide text-muted-foreground"
      >
        {label}
      </label>
      <div className="mt-xs flex flex-wrap items-center gap-sm">
        <input
          id={tokenId}
          ref={inputRef}
          type="password"
          name="operatorToken"
          autoComplete="off"
          spellCheck={false}
          placeholder="Paste the token, then save"
          className={cn(
            'min-w-0 flex-1 rounded-button border border-border bg-muted px-md py-sm font-mono text-sm text-foreground',
            'placeholder:text-muted-foreground',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
          )}
        />
        <Button type="submit" size="sm" variant="secondary" icon={<KeyRound className="size-3.5" />}>
          Save token
        </Button>
        {tokenSaved ? (
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={handleClear}
            icon={<Trash2 className="size-3.5" />}
          >
            Clear token
          </Button>
        ) : null}
      </div>
      <p role="status" aria-live="polite" className="mt-xs text-xs text-muted-foreground">
        {tokenSaved ? TOKEN_SAVED_MESSAGE : TOKEN_MISSING_MESSAGE}
      </p>
      {hint === undefined || hint === '' ? null : (
        <p className="mt-xs text-xs text-muted-foreground">{hint}</p>
      )}
    </form>
  );
}
