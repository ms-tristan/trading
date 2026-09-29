"use client";

import { useRouter } from "next/navigation";
import { useEffect, useId, useRef, useState } from "react";

import { Card } from "@/components/ui/Card";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { readOperatorToken } from "@/components/ui/TokenPrompt";
import { postKillSwitch } from "@/lib/api";
import { cn } from "@/lib/cn";

export interface KillSwitchControlProps {
  /** State the API reports: `true` when the switch is engaged. */
  engaged: boolean;
  /** Operator token held by the page; read from the session storage when absent. */
  token: string | null;
  className?: string;
}

/**
 * Global kill switch.
 *
 * It is the most destructive control of the dashboard, so it is a **two-step**
 * one: the first click only opens an inline confirmation panel and moves the
 * focus onto the confirming button, and `POST /api/kill-switch` is sent by the
 * second one only. Releasing the switch asks for the same confirmation, because
 * it authorises workers to start again.
 */
export function KillSwitchControl({ engaged, token, className }: KillSwitchControlProps) {
  const router = useRouter();
  const [target, setTarget] = useState<boolean | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const descriptionId = useId();

  useEffect(() => {
    if (target !== null) {
      confirmRef.current?.focus();
    }
  }, [target]);

  async function confirm() {
    if (target === null) {
      return;
    }
    setError(null);
    setMessage(null);

    const effectiveToken = token ?? readOperatorToken();
    if (effectiveToken === null) {
      setTarget(null);
      setMessage("Save the operator token of this tab before using the kill switch.");
      return;
    }

    setPending(true);
    try {
      const response = await postKillSwitch(target, effectiveToken);
      // `POST /api/kill-switch` answers the flag only: it does not publish which
      // profiles it stopped, so the report states the position of the switch
      // instead of inventing a count.
      setMessage(
        (response?.engaged ?? target)
          ? "Kill switch engaged; the engine stopped the running workers."
          : "Kill switch released; the engine may start workers again.",
      );
      setTarget(null);
      router.refresh();
    } catch (cause) {
      setTarget(null);
      setError(cause instanceof Error ? cause : new Error(String(cause)));
    } finally {
      setPending(false);
    }
  }

  return (
    <Card
      className={className}
      headingLevel={2}
      title="Kill switch"
      description="Stops every worker and refuses to start a new one"
      actions={
        <span
          data-engaged={engaged ? "true" : "false"}
          className={cn(
            "rounded-full border px-2 py-0.5 text-sm",
            engaged ? "border-destructive" : "border-border",
          )}
        >
          {engaged ? "Engaged" : "Released"}
        </span>
      }
    >
      <p className="text-sm text-muted-foreground">
        {engaged
          ? "Every worker is stopped and nothing starts until the switch is released."
          : "Engaging it stops every running profile immediately and prevents any worker from starting."}
      </p>

      {target === null ? (
        <button
          type="button"
          disabled={pending}
          onClick={() => {
            setError(null);
            setMessage(null);
            setTarget(!engaged);
          }}
          className="mt-2 rounded-md border border-destructive/60 bg-destructive/10 px-3 py-1 text-base transition-smooth hover:border-ring disabled:cursor-not-allowed disabled:opacity-50"
        >
          {engaged ? "Release kill switch" : "Engage kill switch"}
        </button>
      ) : (
        <div
          role="alertdialog"
          aria-labelledby={titleId}
          aria-describedby={descriptionId}
          className="mt-2 min-w-0 rounded-md border border-destructive/60 bg-destructive/10 p-3"
        >
          <p id={titleId} className="font-semibold">
            {target ? "Engage the kill switch?" : "Release the kill switch?"}
          </p>
          <p id={descriptionId} className="text-sm">
            {target
              ? "Every running profile is stopped now, and nothing starts until you release it."
              : "Workers may start again on the next sweep."}
          </p>
          <div className="mt-2 flex flex-wrap gap-2">
            <button
              ref={confirmRef}
              type="button"
              disabled={pending}
              onClick={() => {
                void confirm();
              }}
              className="rounded-md border border-destructive bg-destructive/20 px-3 py-1 text-base transition-smooth hover:border-ring disabled:cursor-not-allowed disabled:opacity-50"
            >
              {pending
                ? "Sending\u2026"
                : target
                  ? "Confirm engagement"
                  : "Confirm release"}
            </button>
            <button
              type="button"
              onClick={() => setTarget(null)}
              className="rounded-md border border-border bg-secondary px-3 py-1 text-base transition-smooth hover:border-ring"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {message !== null ? (
        <p role="status" className="mt-2 text-sm text-muted-foreground">
          {message}
        </p>
      ) : null}

      <ErrorBanner className="mt-2" error={error} title="The engine refused the kill switch" />
    </Card>
  );
}
