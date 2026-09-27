import type { ReactNode } from "react";

import { Card } from "@/components/ui/Card";
import { StateBadge } from "@/components/ui/StateBadge";
import { formatUsdt } from "@/lib/format";
import type { ProfileConfigView, ProfileView } from "@/lib/types";

export interface ProfileConfigCardProps {
  /** Resolved configuration of the profile (`GET /api/profiles/{id}`). */
  config: ProfileConfigView;
  /** Fallback of the live fields, for a payload that carries no configuration. */
  profile: ProfileView;
  /** Exchange the worker trades on; a wire field the API may not publish. */
  exchange: string | null;
  /** Scheduling priority of the profile; a wire field the API may not publish. */
  priority: number | null;
  className?: string;
}

/** One label/value row of the definition list. */
function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-sm text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}

/** A value the API does not publish renders as an em dash. */
function dash(value: string | number | null): string {
  return value === null || value === "" ? "\u2014" : String(value);
}

/**
 * The configuration the engine resolved for this profile.
 *
 * It is what the operator checks before an incident: which strategy and pairs
 * the worker actually loaded, in which mode and with which capital. Every field
 * falls back to the profile record when the configuration block is empty, so a
 * partial payload still shows the live values instead of a row of dashes.
 */
export function ProfileConfigCard({
  config,
  profile,
  exchange,
  priority,
  className,
}: ProfileConfigCardProps) {
  const timeframe = config.timeframe !== "" ? config.timeframe : profile.timeframe;
  const pairs = config.pairs.length > 0 ? config.pairs : profile.pairs;
  const strategyTitle = profile.strategy_title !== "" ? profile.strategy_title : config.strategy;
  const initialCapital =
    Number.isFinite(config.initial_capital) && config.initial_capital !== 0
      ? config.initial_capital
      : profile.initial_capital;
  const stakeAmount = Number.isFinite(config.stake_amount) ? config.stake_amount : Number.NaN;

  return (
    <Card
      className={className}
      headingLevel={2}
      title="Resolved configuration"
      description="What the engine loaded for this profile"
    >
      <dl className="grid grid-cols-1 gap-x-4 gap-y-2 sm:grid-cols-2 lg:grid-cols-3">
        <Row label="Strategy">
          {dash(strategyTitle)}
          {config.strategy !== "" ? (
            <span className="ml-1 font-mono text-sm text-muted-foreground">{config.strategy}</span>
          ) : null}
        </Row>
        <Row label="Timeframe">
          <span className="tabular-nums">{dash(timeframe)}</span>
        </Row>
        <Row label="Mode">
          <span className="uppercase">{config.mode}</span>
          <span className="ml-1 text-sm text-muted-foreground">
            {config.dry_run ? "dry run" : "real funds"}
          </span>
        </Row>
        <Row label="Exchange">{dash(exchange)}</Row>
        <Row label="Pairs">
          {pairs.length === 0 ? "\u2014" : <span className="font-mono text-sm">{pairs.join(", ")}</span>}
        </Row>
        <Row label="Initial capital">
          <span className="tabular-nums">{formatUsdt(initialCapital)}</span>
        </Row>
        <Row label="Max open trades">
          <span className="tabular-nums">{dash(config.max_open_trades)}</span>
        </Row>
        <Row label="Stake amount">
          <span className="tabular-nums">{formatUsdt(stakeAmount)}</span>
        </Row>
        <Row label="Priority">
          <span className="tabular-nums">{dash(priority)}</span>
        </Row>
        <Row label="State">
          <StateBadge state={profile.state} reason={profile.state_reason} />
        </Row>
        <Row label="Engine slot">
          <span className="tabular-nums">{dash(profile.engine_slot)}</span>
        </Row>
        <Row label="API port">
          <span className="tabular-nums">{dash(profile.api_port)}</span>
        </Row>
        <Row label="Startable">
          {config.startable ? "Yes" : "No"}
          {config.startable ? null : (
            <span className="ml-1 text-sm text-muted-foreground">
              the engine refuses to start it right now
            </span>
          )}
        </Row>
      </dl>
    </Card>
  );
}
