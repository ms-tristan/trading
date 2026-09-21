'use client';

/**
 * Operator token form: the single save / verify / clear surface of the token.
 *
 * It is rendered by every operator-facing page that needs a token — the
 * kill-switch panel of the overview and the profile creation form — so saving
 * the token is never reachable from one page only. Both surfaces show exactly
 * the same control and report the same sentences, because they render this very
 * component.
 *
 * Verification
 * ------------
 * Saving a token only proves it was *stored*, never that it is *right*: the
 * server answers the same `403 missing or invalid operator token` whether the
 * header was absent, wrong, or mutations are disabled, so an operator who
 * pasted a stale token learns nothing until a mutation fails. The **Verify**
 * action calls `GET /api/operator-token`, which answers the question directly,
 * and reports one of four distinct outcomes — valid, wrong, nothing supplied,
 * server without a token. A wrong token reports `valid: false` on a `200`
 * rather than throwing: it is an answer, not an error.
 *
 * Security rules that this component implements:
 *
 * * the token lives in `sessionStorage` only — never `localStorage` (it would
 *   survive the tab), never a cookie (it would be sent with every request) and
 *   never a URL. The storage helpers of `@/lib/operator-token` enforce it;
 * * the value is submitted through a `type="password"` input whose value is
 *   cleared from the DOM immediately after saving, is never mirrored into React
 *   state, never rendered back, never logged and never placed in an error
 *   message. The status line only ever reports *whether* a token is stored ;
 * * verification reads the token from `sessionStorage` (never from an input),
 *   so a verified secret is never held in component state either ;
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

import { useCallback, useId, useRef, useState, useSyncExternalStore } from 'react';
import type { FormEvent, JSX } from 'react';

import { KeyRound, ShieldCheck, Trash2 } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { verifyOperatorToken } from '@/lib/api';
import { failureReport } from '@/lib/api-failure';
import { cn } from '@/lib/cn';
import { clearOperatorToken, hasOperatorToken, readOperatorToken, saveOperatorToken } from '@/lib/operator-token';

/** Status line shown while a token is stored in this tab. */
export const TOKEN_SAVED_MESSAGE =
  'Token saved for this tab (session storage only). It is never shown again.';

/** Status line shown while no token is stored in this tab. */
export const TOKEN_MISSING_MESSAGE = 'No token saved in this tab.';

/** Reported when the server confirms the stored token authorises mutations. */
export const TOKEN_VERIFIED_MESSAGE = 'This token is valid: the server accepts it.';

/** Reported when the server refuses the stored token. */
export const TOKEN_REJECTED_MESSAGE =
  'The saved token is not accepted by the server. Paste the current token and save it again.';

/** Reported when nothing is stored, so there is nothing to verify. */
export const TOKEN_NOTHING_TO_VERIFY_MESSAGE = 'Save a token before verifying it.';

/** Reported when the server itself has no token configured. */
export const TOKEN_DISABLED_MESSAGE =
  'This server accepts no operator token, so no token can authorise a change. Check TB_OPERATOR_TOKEN on the server.';

/** Props of {@link OperatorTokenForm}. */
export interface OperatorTokenFormProps {
  /** Extra operator-facing hint rendered under the status line (optional). */
  hint?: string;
  /** Accessible, visible label of the section; defaults to 'Operator token'. */
  label?: string;
  className?: string;
  /** Absolute origin used by Server Components; empty means same origin. */
  baseUrl?: string;
  /** Fetch implementation seam for tests. Defaults to the global `fetch`. */
  fetchImpl?: typeof fetch;
}

/** Outcome of the last verification, rendered next to the buttons. */
type VerifyOutcome =
  | { readonly kind: 'idle' }
  | { readonly kind: 'valid'; readonly message: string }
  | { readonly kind: 'rejected'; readonly message: string }
  | { readonly kind: 'disabled'; readonly message: string }
  | { readonly kind: 'failure'; readonly headline: string; readonly detail: string };

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
 * Map a verification payload onto the message the operator must read.
 *
 * A wrong token and a token-less server are deliberately different sentences:
 * only the first one is fixed by pasting a different token.
 */
function outcomeOf(payload: { valid: boolean; read_only?: boolean }): VerifyOutcome {
  if (payload.valid) {
    return { kind: 'valid', message: TOKEN_VERIFIED_MESSAGE };
  }
  if (payload.read_only === true) {
    return { kind: 'disabled', message: TOKEN_DISABLED_MESSAGE };
  }
  return { kind: 'rejected', message: TOKEN_REJECTED_MESSAGE };
}

/**
 * Save, verify or forget the operator token.
 *
 * The same control is rendered on every surface that needs a token, so the
 * operator can set it, confirm the server accepts it, see whether it is stored
 * and clear it from either page — the storage contract itself is unchanged
 * (`sessionStorage` only).
 */
export function OperatorTokenForm({
  hint,
  label = 'Operator token',
  className,
  baseUrl,
  fetchImpl,
}: OperatorTokenFormProps): JSX.Element {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const tokenId = useId();
  const [verifyOutcome, setVerifyOutcome] = useState<VerifyOutcome>({ kind: 'idle' });
  const [verifying, setVerifying] = useState<boolean>(false);

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
    // A fresh token invalidates the previous verdict: it has not been checked.
    setVerifyOutcome({ kind: 'idle' });
    notifyTokenSaved();
  }, []);

  const handleClear = useCallback(() => {
    clearOperatorToken();
    const input = inputRef.current;
    if (input !== null) {
      input.value = '';
    }
    setVerifyOutcome({ kind: 'idle' });
    notifyTokenSaved();
  }, []);

  /*
   * Verification reads the token from storage rather than from the input: the
   * input is cleared on save, and a verified secret must never be held in
   * component state. The request carries it as a header, never in a URL.
   */
  const handleVerify = useCallback(async (): Promise<void> => {
    const token = readOperatorToken();
    if (token === null) {
      setVerifyOutcome({ kind: 'rejected', message: TOKEN_NOTHING_TO_VERIFY_MESSAGE });
      return;
    }
    setVerifying(true);
    setVerifyOutcome({ kind: 'idle' });
    try {
      const payload = await verifyOperatorToken({ operatorToken: token, baseUrl, fetchImpl });
      setVerifyOutcome(outcomeOf(payload));
    } catch (thrown) {
      // A transport failure is not a verdict on the token: say so instead of
      // claiming the token is wrong.
      const report = failureReport(thrown);
      setVerifyOutcome({ kind: 'failure', headline: report.headline, detail: report.detail });
    } finally {
      setVerifying(false);
    }
  }, [baseUrl, fetchImpl]);

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
            variant="secondary"
            disabled={verifying}
            aria-busy={verifying}
            onClick={() => {
              void handleVerify();
            }}
            icon={<ShieldCheck className="size-3.5" />}
          >
            {verifying ? 'Verifying...' : 'Verify token'}
          </Button>
        ) : null}
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
      {verifyOutcome.kind === 'idle' ? null : (
        <p
          role="status"
          aria-live="polite"
          data-tone={
            verifyOutcome.kind === 'valid'
              ? 'ok'
              : verifyOutcome.kind === 'failure'
                ? 'unknown'
                : 'error'
          }
          className={cn(
            'mt-xs text-xs',
            verifyOutcome.kind === 'valid' ? 'text-profit' : 'text-loss',
          )}
        >
          {verifyOutcome.kind === 'failure'
            ? `${verifyOutcome.headline} · ${verifyOutcome.detail}`
            : verifyOutcome.message}
        </p>
      )}
      {hint === undefined || hint === '' ? null : (
        <p className="mt-xs text-xs text-muted-foreground">{hint}</p>
      )}
    </form>
  );
}
