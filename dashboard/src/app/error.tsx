'use client';

import { ErrorBanner } from '@/components/ui/error-banner';
import { ApiError, errorMessage } from '@/lib/api';
import { failureReport } from '@/lib/api-failure';

/**
 * Route error boundary.
 *
 * A failed Server Component render becomes a non-blocking banner plus a reset
 * button, so the operator can retry without reloading the whole application.
 *
 * When the render failed on a monitoring request, the banner speaks the shared
 * operator-facing vocabulary: a `502`, `503` or `504` from the proxy, a
 * connection failure and a timeout all read as the monitoring API being
 * unreachable, and the raw status stays in the detail line. Any other render
 * fault keeps its own message — this boundary also catches programming errors
 * that have nothing to do with the monitoring API.
 */
export default function RouteError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  const report = error instanceof ApiError ? failureReport(error) : null;

  return (
    <div className="flex flex-col gap-xl">
      <h1 className="font-mono text-xl font-semibold text-foreground">Something went wrong</h1>
      <ErrorBanner
        message={report === null ? errorMessage(error) : report.headline}
        detail={report?.detail ?? null}
        onRetry={reset}
        retryLabel="Try again"
      />
    </div>
  );
}
