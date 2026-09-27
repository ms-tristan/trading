import { ApiError } from "@/lib/api";
import { cn } from "@/lib/cn";

export interface ErrorBannerProps {
  error: Error | null | undefined;
  /** What could not be loaded, e.g. `"Account performance"`. */
  title?: string;
  className?: string;
}

/**
 * Non-blocking error surface: it never replaces the data the page managed to
 * load, it states what failed and on which API path, and it announces itself
 * politely through `role="status"`.
 */
export function ErrorBanner({ error, title = "Unavailable", className }: ErrorBannerProps) {
  if (!error) {
    return null;
  }

  const path = error instanceof ApiError ? error.path : null;
  const status = error instanceof ApiError && error.status > 0 ? error.status : null;

  return (
    <div
      role="status"
      className={cn(
        "min-w-0 rounded-lg border border-destructive/60 bg-destructive/10 p-3 text-base",
        className,
      )}
    >
      <p className="font-semibold text-loss">{title}</p>
      <p className="text-foreground">{error.message}</p>
      <p className="mt-0.5 flex flex-wrap gap-x-3 text-sm text-muted-foreground">
        {path ? (
          <span>
            path <code className="font-mono text-foreground">{path}</code>
          </span>
        ) : null}
        {status !== null ? <span className="tabular-nums">HTTP {status}</span> : null}
      </p>
    </div>
  );
}
