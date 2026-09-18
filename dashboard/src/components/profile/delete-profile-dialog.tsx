'use client';

import { useCallback, useEffect, useId, useRef } from 'react';
import type { KeyboardEvent, MouseEvent } from 'react';

import { Trash2 } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { ErrorBanner } from '@/components/ui/error-banner';
import { cn } from '@/lib/cn';

/** Props of {@link DeleteProfileDialog}. */
export interface DeleteProfileDialogProps {
  /** Whether the dialog is on screen. `false` renders nothing at all. */
  open: boolean;
  /** Identifier of the profile the operator is about to delete. */
  profileId: string;
  /** Whether the delete request is in flight. Disables the confirmation. */
  pending: boolean;
  /** Message of the last failed delete, or `null`. */
  error: string | null;
  /** Send the delete request. Called only by an explicit confirmation. */
  onConfirm: () => void;
  /** Dismiss the dialog without deleting anything. */
  onCancel: () => void;
}

/**
 * Elements a keyboard user can reach with Tab, in document order.
 *
 * Disabled controls are skipped on purpose: a button that cannot be pressed must
 * never take the focus ring away from one that can.
 */
const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(', ');

/** Return the focusable elements of `container`, in document order. */
function focusableWithin(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (element) => element.tabIndex >= 0 && element.getAttribute('aria-hidden') !== 'true',
  );
}

/**
 * Confirmation dialog of the destructive `DELETE /api/profiles/{id}`.
 *
 * Deleting a profile closes every open order and flattens the open position at
 * market, so the action is never a single click: this dialog states the
 * consequence without any euphemism, and the request is sent only by its
 * confirmation button.
 *
 * Accessibility contract:
 *
 * * `role="dialog"`, `aria-modal="true"`, a title and a description bound with
 *   `aria-labelledby` / `aria-describedby`;
 * * the dialog is opened by an explicit gesture, so focus moves into it (the
 *   Cancel button) as soon as it opens, is kept inside it while it is open (Tab
 *   and Shift+Tab wrap around its own controls) and returns to the element that
 *   opened it when it closes;
 * * Escape and a click on the overlay both cancel, so there is always a way out
 *   that is not the destructive button;
 * * the confirmation is visibly destructive, `disabled` while the request is in
 *   flight, and the dialog announces that state with `aria-busy` — a double
 *   submit is impossible;
 * * the server message of a refused delete is rendered inside the dialog, right
 *   next to the action that failed.
 */
export function DeleteProfileDialog({
  open,
  profileId,
  pending,
  error,
  onConfirm,
  onCancel,
}: DeleteProfileDialogProps) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const restoreRef = useRef<HTMLElement | null>(null);
  const titleId = useId();
  const descriptionId = useId();
  const cancelId = useId();

  /*
   * Focus management of the open dialog: the element that opened it is
   * remembered so it can be focused again on close, and the Cancel button takes
   * the focus while the dialog is open. Both happen in the same effect so a
   * dialog that is never opened touches nothing.
   */
  useEffect(() => {
    if (!open) {
      return undefined;
    }
    const previous = document.activeElement;
    restoreRef.current = previous instanceof HTMLElement ? previous : null;
    document.getElementById(cancelId)?.focus();
    return () => {
      const target = restoreRef.current;
      restoreRef.current = null;
      if (target !== null && target.isConnected) {
        target.focus();
      }
    };
  }, [cancelId, open]);

  const handleKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      if (event.key === 'Escape') {
        // The dialog owns the Escape key while it is open.
        event.preventDefault();
        event.stopPropagation();
        onCancel();
        return;
      }
      if (event.key !== 'Tab') {
        return;
      }
      const container = dialogRef.current;
      if (container === null) {
        return;
      }
      const focusables = focusableWithin(container);
      if (focusables.length === 0) {
        event.preventDefault();
        container.focus();
        return;
      }
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement;
      const inside = active instanceof HTMLElement && container.contains(active);
      if (event.shiftKey) {
        if (!inside || active === first) {
          event.preventDefault();
          last.focus();
        }
        return;
      }
      if (!inside || active === last) {
        event.preventDefault();
        first.focus();
      }
    },
    [onCancel],
  );

  const handleOverlayClick = useCallback(
    (event: MouseEvent<HTMLDivElement>) => {
      // Only a click on the backdrop itself cancels: a click that started inside
      // the dialog and ended on the backdrop must not dismiss it.
      if (event.target === event.currentTarget) {
        onCancel();
      }
    },
    [onCancel],
  );

  if (!open) {
    return null;
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 p-xl motion-safe:transition-opacity motion-safe:duration-200"
      onClick={handleOverlayClick}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        aria-busy={pending}
        tabIndex={-1}
        onKeyDown={handleKeyDown}
        className={cn(
          'w-full max-w-md rounded-dialog border border-border bg-card p-2xl text-card-foreground shadow-xl',
          'motion-safe:transition-transform motion-safe:duration-200',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
        )}
      >
        <h2 id={titleId} className="font-mono text-base font-semibold text-card-foreground">
          {`Delete profile ${profileId}?`}
        </h2>
        <p id={descriptionId} className="mt-sm text-sm text-muted-foreground">
          Every open order will be cancelled and the open position will be closed at market before
          the profile is removed. This cannot be undone.
        </p>

        <ErrorBanner message={error} className="mt-lg" />

        <div className="mt-xl flex flex-wrap justify-end gap-sm">
          <Button id={cancelId} variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
          <Button
            variant="danger"
            onClick={onConfirm}
            disabled={pending}
            aria-busy={pending}
            icon={<Trash2 className="size-4" />}
          >
            Delete profile
          </Button>
        </div>
      </div>
    </div>
  );
}
