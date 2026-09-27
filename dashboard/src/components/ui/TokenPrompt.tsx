"use client";

import { useEffect, useId, useRef, useState, useSyncExternalStore, type FormEvent } from "react";

import { cn } from "@/lib/cn";

/**
 * `sessionStorage` key of the operator token.
 *
 * The token lives in the session storage of the current tab only: never in
 * `localStorage` (which would outlive the tab), never in a cookie (which would
 * travel with every request) and never in the server-rendered HTML.
 */
export const OPERATOR_TOKEN_STORAGE_KEY = "trading.operator-token";

/** Subscribers of the token store, so React re-renders when it changes. */
const subscribers = new Set<() => void>();

function emitTokenChange(): void {
  for (const subscriber of subscribers) {
    subscriber();
  }
}

function subscribeToToken(listener: () => void): () => void {
  subscribers.add(listener);
  return () => {
    subscribers.delete(listener);
  };
}

/** Read the operator token of this tab, or `null`. */
export function readOperatorToken(): string | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    const value = window.sessionStorage.getItem(OPERATOR_TOKEN_STORAGE_KEY);
    return value !== null && value.trim() !== "" ? value : null;
  } catch {
    return null;
  }
}

/** Store the operator token for this tab. */
export function saveOperatorToken(token: string): void {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.sessionStorage.setItem(OPERATOR_TOKEN_STORAGE_KEY, token);
  } catch {
    // A tab with storage disabled simply keeps the token in memory.
  }
  emitTokenChange();
}

/** Forget the operator token of this tab. */
export function clearOperatorToken(): void {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.sessionStorage.removeItem(OPERATOR_TOKEN_STORAGE_KEY);
  } catch {
    // nothing to forget
  }
  emitTokenChange();
}

/** Server snapshot: the markup never carries the token. */
function serverToken(): null {
  return null;
}

export interface TokenPromptProps {
  /** Called with the stored token, or `null` once it is forgotten. */
  onTokenChange?: (token: string | null) => void;
  className?: string;
}

/**
 * Ask for the operator token once and keep it for the session.
 *
 * The token is read from `sessionStorage` through `useSyncExternalStore`, so the
 * server renders the empty form and the client renders the stored state without
 * a hydration mismatch. The stored token is never rendered back: once saved, the
 * prompt only says that a token is held, and offers to forget it.
 */
export function TokenPrompt({ onTokenChange, className }: TokenPromptProps) {
  const token = useSyncExternalStore(subscribeToToken, readOperatorToken, serverToken);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const inputId = useId();

  const callbackRef = useRef(onTokenChange);
  useEffect(() => {
    callbackRef.current = onTokenChange;
  }, [onTokenChange]);

  useEffect(() => {
    // Announces the value of the session to the caller; it never writes state.
    callbackRef.current?.(token);
  }, [token]);

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = draft.trim();
    if (value === "") {
      setError("Enter the operator token.");
      return;
    }
    saveOperatorToken(value);
    setDraft("");
    setError(null);
  }

  function handleForget() {
    clearOperatorToken();
    setDraft("");
    setError(null);
  }

  return (
    <div className={cn("min-w-0 rounded-lg border border-border bg-card p-3", className)}>
      <p className="text-base font-semibold">Operator token</p>
      {token === null ? (
        <form className="mt-2 flex flex-wrap items-end gap-2" onSubmit={handleSubmit}>
          <label className="min-w-0 text-sm text-muted-foreground" htmlFor={inputId}>
            Token (kept in this tab only)
            <input
              id={inputId}
              type="password"
              value={draft}
              autoComplete="off"
              aria-describedby={`${inputId}-hint`}
              onChange={(event) => setDraft(event.target.value)}
              className="mt-1 block w-full min-w-[12rem] rounded-md border border-border bg-background px-2 py-1 text-foreground transition-smooth"
            />
          </label>
          <button
            type="submit"
            className="rounded-md border border-border bg-secondary px-3 py-1 text-base transition-smooth hover:border-ring"
          >
            Save token
          </button>
          <p id={`${inputId}-hint`} className="w-full text-sm text-muted-foreground">
            Kept in the session storage of this tab: never in local storage, never in a cookie and
            never displayed again.
          </p>
        </form>
      ) : (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <p className="text-sm text-muted-foreground">
            An operator token is held for this tab; mutating calls carry it in the{" "}
            <code className="font-mono text-foreground">X-Operator-Token</code> header.
          </p>
          <button
            type="button"
            onClick={handleForget}
            className="rounded-md border border-border bg-secondary px-3 py-1 text-base transition-smooth hover:border-ring"
          >
            Forget token
          </button>
        </div>
      )}
      {error !== null ? (
        <p role="alert" className="mt-2 text-sm text-loss">
          {error}
        </p>
      ) : null}
    </div>
  );
}
