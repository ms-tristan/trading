import {
  Activity,
  Banknote,
  ChartLine,
  Coins,
  FlaskConical,
  Gauge,
  Layers,
  OctagonAlert,
  Radio,
  Server,
  ShieldCheck,
  Timer,
  TriangleAlert,
} from 'lucide-react';
import type { ReactNode } from 'react';

import { StatTile, type StatTrend } from '@/components/ui/stat-tile';
import { StatusBadge, profileStatusTone } from '@/components/ui/status-badge';
import { cn } from '@/lib/cn';
import {
  EMPTY_PLACEHOLDER,
  formatDuration,
  formatInteger,
  formatMoney,
  formatRatioAsPercent,
  formatTimestamp,
  trendOf,
} from '@/lib/format';
import type { HealthPayload, KillSwitchPayload, ProfileSnapshot } from '@/lib/types';

/** Props of {@link ProfileHeader}. */
export interface ProfileHeaderProps {
  /** Snapshot of the profile (`GET /api/profiles/{id}`). */
  profile: ProfileSnapshot;
  /** Platform health (`GET /api/health`). */
  health: HealthPayload;
  /** Kill-switch state (`GET /api/kill-switch`). */
  killSwitch: KillSwitchPayload;
  className?: string;
}

/** Render a string field, or the em dash placeholder when it is absent. */
function textOrPlaceholder(value: string | null | undefined): string {
  if (typeof value !== 'string') {
    return EMPTY_PLACEHOLDER;
  }
  const trimmed = value.trim();
  return trimmed === '' ? EMPTY_PLACEHOLDER : trimmed;
}

/** Readable label of a snake_case token (`partially_filled` -> `Partially filled`). */
function humaniseToken(value: string): string {
  const words = value.replace(/[_-]+/g, ' ').trim();
  return words === '' ? EMPTY_PLACEHOLDER : words.charAt(0).toUpperCase() + words.slice(1);
}

/** One labelled row of a definition list. */
function Row({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-md gap-y-xs border-b border-border/60 py-sm">
      <dt className="text-xs uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="font-mono text-sm tabular-nums text-foreground">{value}</dd>
    </div>
  );
}

/** Mode of a profile as a labelled, iconised badge. */
function ModeBadge({ mode }: { mode: ProfileSnapshot['mode'] }) {
  const isLive = mode === 'live';
  return (
    <StatusBadge
      label={isLive ? 'Live trading' : 'Paper trading'}
      tone={isLive ? 'warn' : 'info'}
      icon={
        isLive ? <Radio className="size-3.5" /> : <FlaskConical className="size-3.5" />
      }
    />
  );
}

/**
 * Identity and platform state of one profile.
 *
 * Everything the API exposes about the profile and the platform it runs on is
 * here: identity, live snapshot, lifecycle, engine counters and platform health.
 * The status is always a badge with a tone, an icon **and** a text label, and a
 * trend always pairs its colour with a direction word, so no meaning rests on
 * colour alone.
 */
export function ProfileHeader({ profile, health, killSwitch, className }: ProfileHeaderProps) {
  const counters = profile.health.counters;
  const returnTrend: StatTrend = trendOf(profile.total_return);
  const isDegraded = health.status === 'degraded';

  return (
    <section aria-label={`Profile ${profile.profile_id}`} className={cn('flex flex-col gap-xl', className)}>
      <header className="flex flex-col gap-lg rounded-card border border-border bg-card p-xl text-card-foreground shadow-md">
        <div className="flex flex-wrap items-start justify-between gap-lg">
          <div className="min-w-0">
            <h1 className="font-mono text-xl font-semibold text-card-foreground">
              {textOrPlaceholder(profile.profile_id)}
            </h1>
            <p className="mt-xs font-mono text-sm text-muted-foreground">
              {`${textOrPlaceholder(profile.symbol)} · ${textOrPlaceholder(profile.timeframe)} · ${textOrPlaceholder(profile.strategy)}`}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-sm">
            <StatusBadge
              label={humaniseToken(String(profile.status))}
              tone={profileStatusTone(profile.status)}
            />
            <ModeBadge mode={profile.mode} />
            <StatusBadge
              label={isDegraded ? 'Platform degraded' : 'Platform healthy'}
              tone={isDegraded ? 'warn' : 'ok'}
              icon={isDegraded ? <TriangleAlert className="size-3.5" /> : undefined}
            />
            <StatusBadge
              label={killSwitch.kill_switch ? 'Kill switch engaged' : 'Kill switch released'}
              tone={killSwitch.kill_switch ? 'error' : 'neutral'}
              icon={
                killSwitch.kill_switch ? (
                  <OctagonAlert className="size-3.5" />
                ) : (
                  <ShieldCheck className="size-3.5" />
                )
              }
            />
          </div>
        </div>

        <div className="grid grid-cols-1 gap-lg sm:grid-cols-2 lg:grid-cols-4">
          <StatTile
            label="Equity"
            value={formatMoney(profile.equity)}
            icon={<ChartLine className="size-4" />}
          />
          <StatTile
            label="Cash"
            value={formatMoney(profile.cash)}
            icon={<Coins className="size-4" />}
          />
          <StatTile
            label="Position value"
            value={formatMoney(profile.position_value)}
            icon={<Layers className="size-4" />}
          />
          <StatTile
            label="Total return"
            value={formatRatioAsPercent(profile.total_return, { signed: true })}
            trend={returnTrend}
            hint={`Over ${formatInteger(profile.n_trades)} trades`}
          />
          <StatTile
            label="Trades"
            value={formatInteger(profile.n_trades)}
            icon={<Activity className="size-4" />}
          />
          <StatTile
            label="Open positions"
            value={formatInteger(profile.open_positions)}
            icon={<Gauge className="size-4" />}
          />
          <StatTile
            label="Initial balance"
            value={formatMoney(profile.initial_balance)}
            icon={<Banknote className="size-4" />}
          />
          <StatTile
            label="Candle lag"
            value={formatDuration(profile.health.lag_seconds)}
            icon={<Timer className="size-4" />}
            hint={`Last candle ${formatTimestamp(profile.health.last_candle_at)}`}
          />
        </div>
      </header>

      {profile.health.last_error !== null && profile.health.last_error.trim() !== '' ? (
        <p
          role="status"
          className="flex items-start gap-sm rounded-card border border-warn/40 bg-warn/10 px-lg py-md text-sm text-foreground"
        >
          <TriangleAlert aria-hidden="true" className="mt-xs size-4 shrink-0 text-warn" />
          <span>
            <span className="font-medium text-warn">Last error </span>
            <span className="break-words font-mono text-xs">{profile.health.last_error}</span>
          </span>
        </p>
      ) : null}

      <div className="grid grid-cols-1 gap-xl lg:grid-cols-3">
        <section
          aria-label="Lifecycle"
          className="rounded-card border border-border bg-card p-xl text-card-foreground shadow-sm"
        >
          <h2 className="font-mono text-sm font-semibold text-card-foreground">Lifecycle</h2>
          <dl className="mt-md">
            <Row label="Status" value={humaniseToken(String(profile.status))} />
            <Row label="Mode" value={humaniseToken(String(profile.mode))} />
            <Row label="Strategy" value={textOrPlaceholder(profile.strategy)} />
            <Row label="Symbol" value={textOrPlaceholder(profile.symbol)} />
            <Row label="Timeframe" value={textOrPlaceholder(profile.timeframe)} />
            <Row label="Started at" value={formatTimestamp(profile.started_at)} />
            <Row label="Updated at" value={formatTimestamp(profile.updated_at)} />
            <Row label="Last candle at" value={formatTimestamp(profile.health.last_candle_at)} />
            <Row label="Lag" value={formatDuration(profile.health.lag_seconds)} />
            <Row label="Stream reconnects" value={formatInteger(profile.health.reconnect_count)} />
          </dl>
        </section>

        <section
          aria-label="Engine counters"
          className="rounded-card border border-border bg-card p-xl text-card-foreground shadow-sm"
        >
          <h2 className="font-mono text-sm font-semibold text-card-foreground">Engine counters</h2>
          <dl className="mt-md">
            <Row label="Candles processed" value={formatInteger(counters.candles_processed)} />
            <Row label="Orders submitted" value={formatInteger(counters.orders_submitted)} />
            <Row label="Orders filled" value={formatInteger(counters.orders_filled)} />
            <Row label="Orders rejected" value={formatInteger(counters.orders_rejected)} />
            <Row label="Stream reconnects" value={formatInteger(counters.stream_reconnects)} />
            <Row label="Risk rejections" value={formatInteger(counters.risk_rejections)} />
            <Row label="Errors" value={formatInteger(counters.errors)} />
          </dl>
        </section>

        <section
          aria-label="Platform"
          className="rounded-card border border-border bg-card p-xl text-card-foreground shadow-sm"
        >
          <h2 className="flex items-center gap-sm font-mono text-sm font-semibold text-card-foreground">
            <Server aria-hidden="true" className="size-4 text-muted-foreground" />
            Platform
          </h2>
          <dl className="mt-md">
            <Row label="Version" value={textOrPlaceholder(health.version)} />
            <Row label="Uptime" value={formatDuration(health.uptime_seconds)} />
            <Row
              label="Profiles running / total"
              value={`${formatInteger(health.profiles_running)} / ${formatInteger(health.profiles_total)}`}
            />
            <Row label="Checked at" value={formatTimestamp(health.checked_at)} />
            <Row label="Kill switch" value={killSwitch.kill_switch ? 'Engaged' : 'Released'} />
            <Row label="Kill switch reason" value={textOrPlaceholder(killSwitch.reason)} />
            <Row label="Kill switch changed" value={formatTimestamp(killSwitch.changed_at)} />
          </dl>
        </section>
      </div>
    </section>
  );
}
