"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { DEFAULT_REFRESH_INTERVAL_SECONDS } from "@/lib/types";

export interface AutoRefreshProps {
  /** Polling interval in seconds; comes from `GET /api/settings`. */
  intervalSeconds?: number;
  enabled?: boolean;
  /** Renders the visible caption; `false` renders nothing at all. */
  showLabel?: boolean;
}

/**
 * Refresh the server components of the current route on an interval.
 *
 * `router.refresh()` re-renders the React tree on the server and patches it in:
 * it is never a full page reload, so the scroll position and the open panels
 * survive. The timer is paused while the tab is not visible (no pointless API
 * traffic for a background tab) and cleared on unmount.
 */
export function AutoRefresh({
  intervalSeconds = DEFAULT_REFRESH_INTERVAL_SECONDS,
  enabled = true,
  showLabel = true,
}: AutoRefreshProps) {
  const router = useRouter();

  useEffect(() => {
    if (!enabled || !Number.isFinite(intervalSeconds) || intervalSeconds <= 0) {
      return;
    }
    const timer = window.setInterval(() => {
      if (typeof document !== "undefined" && document.visibilityState !== "visible") {
        return;
      }
      router.refresh();
    }, intervalSeconds * 1000);
    return () => window.clearInterval(timer);
  }, [enabled, intervalSeconds, router]);

  if (!showLabel) {
    return null;
  }

  return (
    <p className="text-sm text-muted-foreground tabular-nums">
      {enabled && intervalSeconds > 0
        ? `Auto-refresh every ${intervalSeconds} s`
        : "Auto-refresh paused"}
    </p>
  );
}
