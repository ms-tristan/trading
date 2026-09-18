'use client';

import { Pause, Play, RefreshCw } from 'lucide-react';

import { cn } from '@/lib/cn';
import { formatTimestamp } from '@/lib/format';
import { Button } from './button';
import { ErrorBanner } from './error-banner';

/** Props of {@link LiveToolbar}. */
export interface LiveToolbarProps {
  /** ISO-8601 stamp of the last successful poll (server or client), or `null`. */
  checkedAt: string | null;
  /** Whether live updates are currently paused. */
  isPaused: boolean;
  /** Pause when live, resume when paused. */
  onToggle: () => void;
  /** Force one polling cycle now. */
  onRefresh: () => void;
  /** Last polling error, shown in a non-blocking banner. */
  error?: string | null;
  /** Name of the live region ('Live updates' by default). */
  liveLabel?: string;
  className?: string;
}

/**
 * The single live-update control of a view: state label, "checked at" stamp,
 * Pause/Resume and Refresh now.
 *
 * Accessibility: the checked-at region is a polite live region, the toggle is a
 * pressed-state button (`aria-pressed`) with a visible text label, and both
 * controls are ordinary buttons, hence fully keyboard operable.
 */
export function LiveToolbar({
  checkedAt,
  isPaused,
  onToggle,
  onRefresh,
  error = null,
  liveLabel = 'Live updates',
  className,
}: LiveToolbarProps) {
  return (
    <div className={cn('flex flex-col gap-sm', className)}>
      <div className="flex flex-wrap items-center justify-between gap-md">
        <p className="flex flex-wrap items-center gap-sm text-sm text-muted-foreground">
          <span
            className={cn(
              'inline-flex items-center gap-xs font-medium',
              isPaused ? 'text-warn' : 'text-profit',
            )}
          >
            <span aria-hidden="true" className="inline-flex items-center">
              {isPaused ? <Pause className="size-3.5" /> : <Play className="size-3.5" />}
            </span>
            {isPaused ? `${liveLabel} paused` : liveLabel}
          </span>
          <span aria-hidden="true">·</span>
          <span aria-live="polite">
            checked at{' '}
            <time dateTime={checkedAt ?? undefined} className="font-mono tabular-nums text-foreground">
              {formatTimestamp(checkedAt)}
            </time>
          </span>
        </p>
        <div className="flex flex-wrap items-center gap-sm">
          <Button
            size="sm"
            variant="secondary"
            onClick={onToggle}
            aria-pressed={isPaused}
            icon={isPaused ? <Play className="size-3.5" /> : <Pause className="size-3.5" />}
          >
            {isPaused ? 'Resume live updates' : 'Pause live updates'}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={onRefresh}
            icon={<RefreshCw className="size-3.5" />}
          >
            Refresh now
          </Button>
        </div>
      </div>
      <ErrorBanner message={error} />
    </div>
  );
}
