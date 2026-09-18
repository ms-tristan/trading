'use client';

import { ErrorBanner } from '@/components/ui/error-banner';
import { errorMessage } from '@/lib/api';

/**
 * Route error boundary.
 *
 * A failed Server Component render becomes a non-blocking banner plus a reset
 * button, so the operator can retry without reloading the whole application.
 */
export default function RouteError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <div className="flex flex-col gap-xl">
      <h1 className="font-mono text-xl font-semibold text-foreground">Something went wrong</h1>
      <ErrorBanner message={errorMessage(error)} onRetry={reset} retryLabel="Try again" />
    </div>
  );
}
