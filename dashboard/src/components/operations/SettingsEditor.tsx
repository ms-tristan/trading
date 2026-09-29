"use client";

import { useRouter } from "next/navigation";
import { useId, useState, type FormEvent } from "react";

import { Card } from "@/components/ui/Card";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { readOperatorToken } from "@/components/ui/TokenPrompt";
import { postSettings } from "@/lib/api";
import type { DashboardSettings, SettingsUpdateRequest } from "@/lib/types";

export interface SettingsEditorProps {
  settings: DashboardSettings;
  /** Operator token held by the page; read from the session storage when absent. */
  token: string | null;
  className?: string;
}

/** Whole number greater than zero, or `null` for an empty or invalid entry. */
export function parsePositiveInteger(value: string): number | null {
  const trimmed = value.trim();
  if (trimmed === "") {
    return null;
  }
  const parsed = Number(trimmed);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}

/** Input value of an optional numeric setting. */
function numberField(value: number | undefined): string {
  return value === undefined || !Number.isFinite(value) ? "" : String(value);
}

/**
 * Editor of the settings an operator changes at run time.
 *
 * A field left empty is not sent, which means "leave it as it is"; a field that
 * carries something else than a whole number greater than zero is rejected
 * locally, before the call. Native form validation is disabled on purpose: the
 * message of a rejected entry is then the English one below, not the browser
 * bubble, and the rule is the same in every browser. The API stays the authority - a missing or wrong
 * operator token comes back as `401`/`403` and is rendered in the banner, and the
 * token itself is only ever read to build the `X-Operator-Token` header.
 */
export function SettingsEditor({ settings, token, className }: SettingsEditorProps) {
  const router = useRouter();
  const [snapshotInterval, setSnapshotInterval] = useState(
    numberField(settings.snapshot_interval_seconds),
  );
  const [validation, setValidation] = useState<string | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const snapshotId = useId();

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setValidation(null);
    setError(null);
    setMessage(null);

    const body: SettingsUpdateRequest = {};
    if (snapshotInterval.trim() !== "") {
      const parsed = parsePositiveInteger(snapshotInterval);
      if (parsed === null) {
        setValidation("Snapshot interval must be a whole number of seconds greater than zero.");
        return;
      }
      body.snapshot_interval_seconds = parsed;
    }
    if (body.snapshot_interval_seconds === undefined) {
      setMessage("Nothing to save: fill in the setting.");
      return;
    }

    const effectiveToken = token ?? readOperatorToken();
    if (effectiveToken === null) {
      setMessage("Save the operator token of this tab before changing a setting.");
      return;
    }

    setSaving(true);
    try {
      const updated = await postSettings(body, effectiveToken);
      const saved = updated ?? {};
      const parts: string[] = [];
      if (body.snapshot_interval_seconds !== undefined) {
        parts.push(
          `snapshot interval ${saved.snapshot_interval_seconds ?? body.snapshot_interval_seconds} s`,
        );
      }
      setMessage(`Saved: ${parts.join(", ")}.`);
      router.refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause : new Error(String(cause)));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card
      className={className}
      headingLevel={2}
      title="Platform settings"
      description="Applied by the engine at run time"
      actions={
        <span className="text-sm text-muted-foreground tabular-nums">
          refresh every {settings.refresh_interval_seconds} s
        </span>
      }
    >
      <form className="grid grid-cols-1 gap-3 sm:grid-cols-2" noValidate onSubmit={submit}>
        <label className="min-w-0 text-sm text-muted-foreground" htmlFor={snapshotId}>
          Snapshot interval (seconds)
          <input
            id={snapshotId}
            type="number"
            min={1}
            step={1}
            inputMode="numeric"
            value={snapshotInterval}
            aria-describedby={`${snapshotId}-hint`}
            onChange={(event) => setSnapshotInterval(event.target.value)}
            className="mt-1 block w-full rounded-md border border-border bg-background px-2 py-1 text-foreground tabular-nums transition-smooth sm:w-32"
          />
          <span id={`${snapshotId}-hint`} className="mt-1 block">
            How often the poller records a portfolio snapshot.
          </span>
        </label>

        <div className="flex flex-wrap items-center gap-2 sm:col-span-2">
          <button
            type="submit"
            disabled={saving}
            className="rounded-md border border-border bg-secondary px-3 py-1 text-base transition-smooth hover:border-ring disabled:cursor-not-allowed disabled:opacity-50"
          >
            {saving ? "Saving\u2026" : "Save settings"}
          </button>
          <span className="text-sm text-muted-foreground">
            Live trading is {settings.allow_live_trading ? "allowed" : "disabled"} on this platform.
          </span>
        </div>
      </form>

      {validation !== null ? (
        <p role="alert" className="mt-2 text-sm text-loss">
          {validation}
        </p>
      ) : null}

      {message !== null ? (
        <p role="status" className="mt-2 text-sm text-muted-foreground">
          {message}
        </p>
      ) : null}

      <ErrorBanner
        className="mt-2"
        error={error}
        title="The engine refused the new settings"
      />
    </Card>
  );
}
