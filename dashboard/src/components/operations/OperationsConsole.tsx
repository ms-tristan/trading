"use client";

import { useState } from "react";

import { TokenPrompt } from "@/components/ui/TokenPrompt";
import { cn } from "@/lib/cn";
import type { DashboardSettings } from "@/lib/types";

import { KillSwitchControl } from "./KillSwitchControl";
import { SettingsEditor } from "./SettingsEditor";

export interface OperationsConsoleProps {
  settings: DashboardSettings;
  /** Kill switch state the API reports. */
  killSwitchEngaged: boolean;
  className?: string;
}

/**
 * The mutating half of the operations page.
 *
 * One `TokenPrompt` serves the whole page: the token is asked for once, kept in
 * the session storage of the tab and read by each control when it is clicked, so
 * neither the settings editor nor the kill switch ever renders it.
 */
export function OperationsConsole({
  settings,
  killSwitchEngaged,
  className,
}: OperationsConsoleProps) {
  const [token, setToken] = useState<string | null>(null);

  return (
    <div className={cn("grid grid-cols-12 gap-2", className)}>
      <TokenPrompt className="col-span-12 xl:col-span-4" onTokenChange={setToken} />
      <SettingsEditor
        className="col-span-12 xl:col-span-4"
        settings={settings}
        token={token}
      />
      <KillSwitchControl
        className="col-span-12 xl:col-span-4"
        engaged={killSwitchEngaged}
        token={token}
      />
    </div>
  );
}
