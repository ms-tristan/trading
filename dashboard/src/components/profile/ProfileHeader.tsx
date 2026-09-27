import type { ReactNode } from "react";

import { StateBadge } from "@/components/ui/StateBadge";
import { cn } from "@/lib/cn";
import { formatTimestamp, formatUsdt } from "@/lib/format";
import type { ProfileView } from "@/lib/types";

export interface ProfileHeaderProps {
  profile: ProfileView;
  /** Title shown when the payload carries no name, e.g. the id of a 404 page. */
  fallbackName?: string;
  /** The start / stop / restart controls of the profile. */
  actions?: ReactNode;
  className?: string;
}

/**
 * Head of the profile page: identity, venue, state and controls.
 *
 * Order of information: the profile name (large), then the strategy title and id,
 * the timeframe, the mode and the traded pairs, then the lifecycle badge **with
 * its reason** as visible text, then the current value and the last update. An
 * empty payload still renders a titled, well-formed page - the name falls back to
 * the requested id - so an unknown profile never shows a blank screen.
 */
export function ProfileHeader({
  profile,
  fallbackName,
  actions,
  className,
}: ProfileHeaderProps) {
  const name = profile.name !== "" ? profile.name : (fallbackName ?? "");
  const strategy = profile.strategy_title !== "" ? profile.strategy_title : profile.strategy;
  const pairs = profile.pairs.join(", ");

  return (
    <section
      aria-labelledby="profile-heading"
      className={cn(
        "min-w-0 rounded-lg border border-border bg-card p-3 text-card-foreground",
        className,
      )}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 id="profile-heading" className="text-2xl font-semibold">
            {name !== "" ? name : "\u2014"}
          </h1>

          <p className="mt-1 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-sm text-muted-foreground">
            {strategy !== "" ? (
              <span className="text-foreground">{strategy}</span>
            ) : null}
            {profile.strategy !== "" && profile.strategy !== strategy ? (
              <code className="break-all font-mono">{profile.strategy}</code>
            ) : null}
            {profile.timeframe !== "" ? (
              <span className="rounded-sm border border-border px-1 tabular-nums">
                {profile.timeframe}
              </span>
            ) : null}
            <span className="rounded-sm border border-border px-1 uppercase">{profile.mode}</span>
            {pairs !== "" ? <span className="break-all font-mono">{pairs}</span> : null}
          </p>

          <div className="mt-2">
            <StateBadge state={profile.state} reason={profile.state_reason} />
          </div>

          <p className="mt-2 text-sm text-muted-foreground tabular-nums">
            portfolio value {formatUsdt(profile.portfolio_value)} - updated{" "}
            {formatTimestamp(profile.updated_at)}
          </p>

          {profile.engine_slot !== null || profile.api_port !== null ? (
            <p className="mt-0.5 text-sm text-muted-foreground tabular-nums">
              {profile.engine_slot !== null ? `engine slot #${profile.engine_slot}` : null}
              {profile.engine_slot !== null && profile.api_port !== null ? " - " : null}
              {profile.api_port !== null ? `freqtrade API port ${profile.api_port}` : null}
            </p>
          ) : null}
        </div>

        {actions ? <div className="w-full min-w-0 lg:w-[24rem]">{actions}</div> : null}
      </div>
    </section>
  );
}
