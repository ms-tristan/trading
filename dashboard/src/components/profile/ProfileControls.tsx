"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { TokenPrompt, readOperatorToken } from "@/components/ui/TokenPrompt";
import { postAction } from "@/lib/api";
import { cn } from "@/lib/cn";
import type { ProfileAction, ProfileState } from "@/lib/types";

export interface ProfileControlsProps {
  profileId: string;
  /** Current lifecycle state, used to grey out the pointless actions. */
  state: ProfileState;
  className?: string;
}

interface ControlSpec {
  action: ProfileAction;
  label: string;
  /** Present continuous of the action, used to report what was sent. */
  progress: string;
  hint: string;
}

const CONTROLS: readonly ControlSpec[] = [
  {
    action: "start",
    label: "Start",
    progress: "is starting",
    hint: "Ask the engine for a slot and run this profile.",
  },
  {
    action: "stop",
    label: "Stop",
    progress: "is stopping",
    hint: "Stop the worker of this profile and release its slot.",
  },
  {
    action: "restart",
    label: "Restart",
    progress: "is restarting",
    hint: "Stop the worker, then start it again.",
  },
];

/**
 * `true` for an action that cannot change anything.
 *
 * Starting a profile that already runs, or stopping one that holds no worker,
 * would only waste a call; the engine stays the authority for everything else -
 * a refusal (a full fleet, a blocked profile) is answered by the API and rendered
 * in the banner.
 */
export function actionDisabled(state: ProfileState, action: ProfileAction): boolean {
  if (action === "start") {
    return state === "running";
  }
  if (action === "stop") {
    return state === "stopped" || state === "error" || state === "blocked";
  }
  return false;
}

/**
 * Lifecycle controls of one profile.
 *
 * The operator token is asked for **once**, by the shared `TokenPrompt`, and
 * lives in the session storage of the tab only: it is never rendered back, never
 * stored on the server and only ever travels in the `X-Operator-Token` header of
 * the mutating call. Every refusal of the API - a missing token (`401`), a wrong
 * one (`403`), a busy fleet (`409`) - is surfaced by the banner, and a successful
 * action re-renders the page so the badge and the metrics follow.
 */
export function ProfileControls({ profileId, state, className }: ProfileControlsProps) {
  const router = useRouter();
  const [token, setToken] = useState<string | null>(null);
  const [pending, setPending] = useState<ProfileAction | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  async function run(control: ControlSpec) {
    setMessage(null);
    setError(null);

    const effectiveToken = token ?? readOperatorToken();
    if (effectiveToken === null) {
      setMessage("Save the operator token of this tab before sending an action.");
      return;
    }

    setPending(control.action);
    try {
      const response = await postAction(profileId, control.action, effectiveToken);
      // `POST /api/profiles/{id}/actions/{action}` answers the refreshed profile
      // view and no message: the sentence is built here from the state the API
      // just reported, never invented for a call that failed.
      const name = response?.profile?.name?.trim();
      setMessage(`${name !== undefined && name !== "" ? name : profileId} ${control.progress}.`);
      router.refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause : new Error(String(cause)));
    } finally {
      setPending(null);
    }
  }

  return (
    <div className={cn("min-w-0 space-y-2", className)}>
      <TokenPrompt onTokenChange={setToken} />

      <div className="flex flex-wrap gap-2">
        {CONTROLS.map((control) => (
          <button
            key={control.action}
            type="button"
            title={control.hint}
            aria-busy={pending === control.action}
            disabled={pending !== null || actionDisabled(state, control.action)}
            onClick={() => {
              void run(control);
            }}
            className="rounded-md border border-border bg-secondary px-3 py-1 text-base transition-smooth hover:border-ring disabled:cursor-not-allowed disabled:opacity-50"
          >
            {pending === control.action ? `${control.label}\u2026` : control.label}
          </button>
        ))}
      </div>

      <p className="text-sm text-muted-foreground">
        Start asks the engine for an engine slot, stop releases it, restart does
        both. An action that cannot change anything is greyed out.
      </p>

      {message !== null ? (
        <p role="status" className="text-sm text-muted-foreground">
          {message}
        </p>
      ) : null}

      <ErrorBanner error={error} title="The engine refused the action" />
    </div>
  );
}
