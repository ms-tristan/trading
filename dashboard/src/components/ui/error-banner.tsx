'use client';

import { CircleAlert, RotateCw } from 'lucide-react';

import { cn } from '@/lib/cn';
import { Button } from './button';

/** Props of {@link ErrorBanner}. */
export interface ErrorBannerProps {
  /** Message to display; `null` renders nothing at all. */
  message: string | null;
  /**
   * Underlying cause of the failure, shown under the message in a secondary
   * style (raw status, server text, requested path). The headline stays the
   * first thing the operator reads.
   */
  detail?: string | null;
  /** Optional retry handler; the button is hidden when absent. */
  onRetry?: () => void;
  /** Label of the retry button. */
  retryLabel?: string;
  className?: string;
}

/**
 * Non-blocking error surface.
 *
 * It never replaces the data on screen: a failed poll shows this banner and the
 * last known good payload stays visible. Announced politely so it does not
 * interrupt what a screen reader is currently reading.
 */
export function ErrorBanner({
  message,
  detail = null,
  onRetry,
  retryLabel = 'Retry',
  className,
}: ErrorBannerProps) {
  if (message === null) {
    return null;
  }
  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        'flex flex-wrap items-center gap-md rounded-card border border-loss/40 bg-loss/10 px-lg py-md text-sm text-foreground',
        className,
      )}
    >
      <CircleAlert aria-hidden="true" className="size-4 shrink-0 text-loss" />
      <span className="min-w-0 flex-1 break-words">{message}</span>
      {detail !== null && detail !== '' ? (
        <span
          data-testid="error-banner-detail"
          className="min-w-0 break-words font-mono text-xs text-muted-foreground"
        >
          {detail}
        </span>
      ) : null}
      {onRetry !== undefined ? (
        <Button
          size="sm"
          variant="secondary"
          onClick={onRetry}
          icon={<RotateCw className="size-3.5" />}
        >
          {retryLabel}
        </Button>
      ) : null}
    </div>
  );
}
