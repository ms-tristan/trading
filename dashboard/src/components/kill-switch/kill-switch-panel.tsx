'use client';

import { useCallback, useEffect, useId, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';

import { OctagonAlert, ShieldCheck } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { ErrorBanner } from '@/components/ui/error-banner';
import { postKillSwitch } from '@/lib/api';
import { failureReport } from '@/lib/api-failure';
import { cn } from '@/lib/cn';
import { EMPTY_PLACEHOLDER, formatTimestamp } from '@/lib/format';
import { readOperatorToken } from '@/lib/operator-token';
import type { KillSwitchPayload } from '@/lib/types';

import { OperatorTokenForm } from './operator-token-form';

/** Props of {@link KillSwitchPanel}. */
export interface KillSwitchPanelProps {
  /** State rendered by the Server Component (the first paint). */
  initialState: KillSwitchPayload;
  /** Live state from the polling hook; wins over the local state once present. */
  state?: KillSwitchPayload | null;
  className?: string;
}

type DialogAction = 'engage' | 'release';

/**
 * Kill-switch control: state, operator token and the engage / release commands.
 *
 * Security and accessibility rules that this panel implements:
 *
 * * the operator token lives in `sessionStorage` only and is owned by
 *   {@link OperatorTokenForm}, which the panel renders like the profile creation
 *   form does — saved, inspected and cleared from either surface. The panel only
 *   shows *whether* a token is stored, never its value;
 * * engaging the emergency stop is destructive, so it always goes through an
 *   explicit confirmation dialog that requires a reason; releasing confirms too;
 * * a failure never throws and never clears the state on screen: it shows a
 *   non-blocking banner and keeps the previous state. The banner carries the
 *   shared operator-facing headline, and the raw cause (status, server text,
 *   requested path) as its detail — only the already-scrubbed `ApiError` of the
 *   API client ever reaches it, so the token can never be echoed back.
 */
export function KillSwitchPanel({ initialState, state = null, className }: KillSwitchPanelProps) {
  const [localState, setLocalState] = useState<KillSwitchPayload>(initialState);
  const [failure, setFailure] = useState<unknown | null>(null);
  const [pending, setPending] = useState<boolean>(false);
  const [dialogAction, setDialogAction] = useState<DialogAction | null>(null);
  const [reason, setReason] = useState<string>('');

  const reasonInputRef = useRef<HTMLInputElement | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);

  const headingId = useId();
  const dialogTitleId = useId();
  const dialogDescriptionId = useId();
  const reasonId = useId();

  const effective = state ?? localState;
  const isEngaged = effective.kill_switch;

  const submit = useCallback(async (engage: boolean, submittedReason: string): Promise<void> => {
    setPending(true);
    setFailure(null);
    try {
      const next = await postKillSwitch(
        { engage, reason: submittedReason },
        { operatorToken: readOperatorToken() ?? '' },
      );
      setLocalState(next);
    } catch (thrown) {
      setFailure(thrown);
    } finally {
      setPending(false);
    }
  }, []);

  const openDialog = useCallback((action: DialogAction, trigger: HTMLButtonElement) => {
    triggerRef.current = trigger;
    setReason('');
    setDialogAction(action);
  }, []);

  const closeDialog = useCallback(() => {
    setDialogAction(null);
    const trigger = triggerRef.current;
    triggerRef.current = null;
    if (trigger !== null) {
      trigger.focus();
    }
  }, []);

  // Focus moves into the dialog as soon as it opens.
  useEffect(() => {
    if (dialogAction === null) {
      return;
    }
    const target = reasonInputRef.current ?? dialogRef.current;
    target?.focus();
  }, [dialogAction]);

  const handleDialogKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        closeDialog();
      }
    },
    [closeDialog],
  );

  const requiresReason = dialogAction === 'engage';
  const canConfirm = !pending && (!requiresReason || reason.trim() !== '');

  const handleConfirm = useCallback(() => {
    if (dialogAction === null || pending) {
      return;
    }
    const trimmed = reason.trim();
    if (dialogAction === 'engage' && trimmed === '') {
      return;
    }
    const engage = dialogAction === 'engage';
    closeDialog();
    void submit(engage, trimmed);
  }, [closeDialog, dialogAction, pending, reason, submit]);

  const reasonText = effective.reason.trim();
  /*
   * A 403 stays readable verbatim: the mapping keeps the server text as the
   * detail (`HTTP 403 · <server text>`), so the operator still learns which of
   * the two documented refusals — read-only mode, invalid token — applies.
   */
  const report = failure === null ? null : failureReport(failure);

  return (
    <section
      aria-labelledby={headingId}
      aria-busy={pending}
      className={cn('rounded-card border border-border bg-card p-xl shadow-md', className)}
    >
      <header className="flex flex-wrap items-start justify-between gap-md">
        <div className="min-w-0">
          <h2 id={headingId} className="font-mono text-base font-semibold text-card-foreground">
            Kill switch
          </h2>
          <p className="mt-xs text-sm text-muted-foreground">
            Emergency stop of every profile. Read-only by default: mutations need the operator
            token.
          </p>
        </div>
        <p
          data-tone={isEngaged ? 'error' : 'ok'}
          data-state={isEngaged ? 'engaged' : 'released'}
          className={cn(
            'inline-flex items-center gap-xs rounded-button border px-md py-xs text-xs font-medium',
            isEngaged
              ? 'border-loss/40 bg-loss/10 text-loss'
              : 'border-profit/40 bg-profit/10 text-profit',
          )}
        >
          <span aria-hidden="true" className="inline-flex shrink-0 items-center">
            {isEngaged ? (
              <OctagonAlert className="size-3.5" />
            ) : (
              <ShieldCheck className="size-3.5" />
            )}
          </span>
          {isEngaged ? 'Engaged' : 'Released'}
        </p>
      </header>

      {isEngaged || reasonText !== '' ? (
        <dl className="mt-lg grid gap-sm text-sm">
          <div className="flex flex-wrap items-baseline gap-sm">
            <dt className="text-xs uppercase tracking-wide text-muted-foreground">Reason</dt>
            <dd className="font-mono text-foreground">
              {reasonText === '' ? EMPTY_PLACEHOLDER : reasonText}
            </dd>
          </div>
          <div className="flex flex-wrap items-baseline gap-sm">
            <dt className="text-xs uppercase tracking-wide text-muted-foreground">Changed at</dt>
            <dd className="font-mono tabular-nums text-foreground">
              {formatTimestamp(effective.changed_at)}
            </dd>
          </div>
        </dl>
      ) : null}

      <OperatorTokenForm className="mt-lg" />

      <div className="mt-lg flex flex-wrap items-center gap-sm">
        <Button
          variant="danger"
          disabled={isEngaged || pending}
          icon={<OctagonAlert className="size-4" />}
          onClick={(event) => openDialog('engage', event.currentTarget)}
        >
          Engage kill switch
        </Button>
        <Button
          variant="secondary"
          disabled={!isEngaged || pending}
          icon={<ShieldCheck className="size-4" />}
          onClick={(event) => openDialog('release', event.currentTarget)}
        >
          Release kill switch
        </Button>
      </div>

      <ErrorBanner
        message={report?.headline ?? null}
        detail={report?.detail ?? null}
        className="mt-lg"
      />

      {dialogAction !== null ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 p-xl">
          <div
            ref={dialogRef}
            role="dialog"
            aria-modal="true"
            aria-labelledby={dialogTitleId}
            aria-describedby={dialogDescriptionId}
            tabIndex={-1}
            onKeyDown={handleDialogKeyDown}
            className="w-full max-w-md rounded-dialog border border-border bg-card p-2xl text-card-foreground shadow-xl"
          >
            <h3 id={dialogTitleId} className="font-mono text-base font-semibold">
              {requiresReason ? 'Engage the kill switch?' : 'Release the kill switch?'}
            </h3>
            <p id={dialogDescriptionId} className="mt-sm text-sm text-muted-foreground">
              {requiresReason
                ? 'Every profile stops trading immediately. This is a destructive action: give a reason, then confirm.'
                : 'Trading resumes on every profile at the next candle. Confirm to release the kill switch.'}
            </p>
            <label
              htmlFor={reasonId}
              className="mt-lg block text-xs font-medium uppercase tracking-wide text-muted-foreground"
            >
              {requiresReason ? 'Reason (required)' : 'Reason (optional)'}
            </label>
            <input
              id={reasonId}
              ref={reasonInputRef}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              className={cn(
                'mt-xs w-full rounded-button border border-border bg-muted px-md py-sm font-mono text-sm text-foreground',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
              )}
            />
            <div className="mt-xl flex flex-wrap justify-end gap-sm">
              <Button variant="ghost" onClick={closeDialog}>
                Cancel
              </Button>
              <Button
                variant={requiresReason ? 'danger' : 'primary'}
                onClick={handleConfirm}
                disabled={!canConfirm}
                icon={
                  requiresReason ? (
                    <OctagonAlert className="size-4" />
                  ) : (
                    <ShieldCheck className="size-4" />
                  )
                }
              >
                {requiresReason ? 'Engage kill switch' : 'Release kill switch'}
              </Button>
            </div>
          </div>
        </div>
      ) : null}
    </section>
  );
}
