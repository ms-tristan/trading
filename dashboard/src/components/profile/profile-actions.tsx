'use client';

import { useCallback, useState, useSyncExternalStore } from 'react';

import { Pause, Play, Trash2 } from 'lucide-react';
import { usePathname, useRouter } from 'next/navigation';

import { DeleteProfileDialog } from '@/components/profile/delete-profile-dialog';
import { Button } from '@/components/ui/button';
import { ErrorBanner } from '@/components/ui/error-banner';
import { StatusBadge } from '@/components/ui/status-badge';
import { cn } from '@/lib/cn';
import { hasOperatorToken } from '@/lib/operator-token';
import { useProfileControl } from '@/lib/use-profile-control';

/** Props of {@link ProfileActions}. */
export interface ProfileActionsProps {
  /** Identifier of the profile these actions drive. */
  profileId: string;
  className?: string;
}

/*
 * `sessionStorage` is a browser-only external store, read through
 * `useSyncExternalStore` (the same pattern as the hook that owns the control
 * state): the server snapshot is always `false`, so a token saved in this tab
 * never causes a hydration mismatch.
 */
const tokenListeners = new Set<() => void>();

function subscribeToken(listener: () => void): () => void {
  tokenListeners.add(listener);
  return () => {
    tokenListeners.delete(listener);
  };
}

function readTokenSnapshot(): boolean {
  return hasOperatorToken();
}

function readTokenServerSnapshot(): boolean {
  return false;
}

/** Subset of the App Router instance this island uses. */
interface DashboardRouter {
  refresh: () => void;
  push: (href: string) => void;
}

/**
 * Resolve the App Router instance, tolerating a renderer without one.
 *
 * `next/navigation` is always mounted in the application, but the island is also
 * rendered by isolated renderers (a component test, an embedded preview) where
 * the router context may be absent. Navigation is a *secondary* effect of a
 * successful delete — the delete itself already happened on the server — so a
 * missing router degrades to a no-op instead of taking the page down. The hooks
 * are still called on every render, in an unchanging order.
 */
function useSafeRouter(): DashboardRouter | null {
  try {
    return useRouter();
  } catch {
    return null;
  }
}

/** Read the current pathname, or `null` when there is no router context. */
function useSafePathname(): string | null {
  try {
    return usePathname() ?? null;
  } catch {
    return null;
  }
}

/**
 * Lifecycle actions of one profile: pause, resume and delete.
 *
 * The island is the **only** control surface of a profile, so the overview card
 * and the profile detail header show exactly the same affordances and share one
 * definition of "what can I do right now":
 *
 * * every state read comes from {@link useProfileControl} — the island never
 *   re-implements polling, mutation or the operator-token rules;
 * * the token itself never leaves `sessionStorage`: the island only reads
 *   *whether* one is stored, and says so instead of failing silently;
 * * a paused profile is visibly paused (badge, plain sentence) and the open
 *   position is explicitly still managed;
 * * one action at a time: while a mutation is in flight every button is
 *   disabled and the button that started it announces `aria-busy`;
 * * the delete is a two-step action: the first click only opens the confirmation
 *   dialog, which is the only place that sends the request;
 * * a successful delete refreshes the Server Component tree, and leaves the
 *   now-deleted detail route for the overview.
 */
export function ProfileActions({ profileId, className }: ProfileActionsProps) {
  const router = useSafeRouter();
  const pathname = useSafePathname();
  const hasToken = useSyncExternalStore(
    subscribeToken,
    readTokenSnapshot,
    readTokenServerSnapshot,
  );
  const [isDeleteOpen, setDeleteOpen] = useState(false);

  const handleDeleted = useCallback(
    (deletedId: string): void => {
      router?.refresh();
      if (pathname === `/profiles/${deletedId}`) {
        // The route no longer exists: the overview is the only safe destination.
        router?.push('/');
      }
    },
    [pathname, router],
  );

  const { paused, pendingAction, error, notice, canPause, canResume, canDelete, pause, resume, remove } =
    useProfileControl(profileId, { onDeleted: handleDeleted });

  const handlePause = useCallback((): void => {
    void pause();
  }, [pause]);

  const handleResume = useCallback((): void => {
    void resume();
  }, [resume]);

  const handleOpenDelete = useCallback((): void => {
    // Nothing destructive happens here: the dialog owns the request.
    setDeleteOpen(true);
  }, []);

  const handleCancelDelete = useCallback((): void => {
    setDeleteOpen(false);
  }, []);

  const handleConfirmDelete = useCallback((): void => {
    void remove().then((deleted) => {
      if (deleted) {
        setDeleteOpen(false);
      }
    });
  }, [remove]);

  return (
    <div
      data-paused={paused ? 'true' : 'false'}
      className={cn('flex flex-wrap items-center gap-md', className)}
    >
      {paused ? (
        <>
          <StatusBadge label="Paused" tone="warn" />
          <p className="min-w-0 text-sm text-muted-foreground">
            Not opening new positions; the open position stays managed.
          </p>
        </>
      ) : null}

      <Button
        variant="secondary"
        icon={<Pause className="size-4" />}
        disabled={!canPause}
        aria-busy={pendingAction === 'pause'}
        onClick={handlePause}
      >
        Pause
      </Button>
      <Button
        variant="primary"
        icon={<Play className="size-4" />}
        disabled={!canResume}
        aria-busy={pendingAction === 'resume'}
        onClick={handleResume}
      >
        Resume
      </Button>
      <Button
        variant="danger"
        icon={<Trash2 className="size-4" />}
        disabled={!canDelete}
        aria-busy={pendingAction === 'delete'}
        onClick={handleOpenDelete}
      >
        Delete profile
      </Button>

      {notice !== null ? (
        <p role="status" aria-live="polite" className="min-w-0 text-sm text-foreground">
          {notice}
        </p>
      ) : null}

      {!hasToken ? (
        <p className="min-w-0 text-sm text-muted-foreground">
          Save an operator token to pause, resume or delete a profile.
        </p>
      ) : null}

      {/*
        Inside the dialog the very same failure is rendered next to the button
        that caused it, so the banner is not duplicated while it is open.
      */}
      <ErrorBanner message={isDeleteOpen ? null : error} className="w-full" />

      <DeleteProfileDialog
        open={isDeleteOpen}
        profileId={profileId}
        pending={pendingAction === 'delete'}
        error={isDeleteOpen ? error : null}
        onConfirm={handleConfirmDelete}
        onCancel={handleCancelDelete}
      />
    </div>
  );
}
