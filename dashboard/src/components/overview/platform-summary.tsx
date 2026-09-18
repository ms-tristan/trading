import type { ReactNode } from 'react';

import { CircleCheck, Layers, ShieldAlert, ShieldCheck, Tag, Timer, TriangleAlert } from 'lucide-react';

import { StatTile } from '@/components/ui/stat-tile';
import { StatusBadge, type StatusTone } from '@/components/ui/status-badge';
import { EMPTY_PLACEHOLDER, formatDuration, formatInteger, formatTimestamp } from '@/lib/format';
import type { HealthPayload, KillSwitchPayload, ProfileSnapshot } from '@/lib/types';

/** Props of {@link PlatformSummary}. */
export interface PlatformSummaryProps {
  /** Payload of `GET /api/health` (status, version, uptime, counters). */
  health: HealthPayload;
  /** Payload of `GET /api/kill-switch` (the platform-wide emergency state). */
  killSwitch: KillSwitchPayload;
  /** Profiles listed on this page, used for the "listed" hint. */
  profiles: ProfileSnapshot[];
}

const HEALTH_LABELS: Record<HealthPayload['status'], string> = {
  ok: 'Healthy',
  degraded: 'Degraded',
};

const HEALTH_TONES: Record<HealthPayload['status'], StatusTone> = {
  ok: 'ok',
  degraded: 'warn',
};

const BADGE_ICON_CLASSES = 'size-3.5';

const HEALTH_ICONS: Record<HealthPayload['status'], ReactNode> = {
  ok: <CircleCheck className={BADGE_ICON_CLASSES} />,
  degraded: <TriangleAlert className={BADGE_ICON_CLASSES} />,
};

/** Render `value`, or the em dash placeholder when it is absent or blank. */
function textOrPlaceholder(value: string | null | undefined): string {
  const trimmed = (value ?? '').trim();
  return trimmed === '' ? EMPTY_PLACEHOLDER : trimmed;
}

/**
 * Platform header of the overview: monitoring-server health, version, uptime,
 * the running-profile count and the kill-switch state.
 *
 * Every state is rendered as a text label with an icon *and* a tone: colour
 * never carries the meaning alone. When the kill switch is engaged the row
 * spells out the degradation reason, so the reason is never hidden behind a
 * hover or a tooltip.
 */
export function PlatformSummary({ health, killSwitch, profiles }: PlatformSummaryProps) {
  const healthLabel = HEALTH_LABELS[health.status] ?? 'Unknown';
  const healthTone = HEALTH_TONES[health.status] ?? 'neutral';
  const isEngaged = killSwitch.kill_switch;
  const reason = killSwitch.reason.trim();

  return (
    <section
      aria-labelledby="platform-summary-heading"
      className="rounded-card border border-border bg-card p-xl text-card-foreground shadow-md"
    >
      <header className="flex flex-wrap items-start justify-between gap-md">
        <div className="min-w-0">
          <h2
            id="platform-summary-heading"
            className="font-mono text-base font-semibold text-card-foreground"
          >
            Platform
          </h2>
          <p className="mt-xs text-sm text-muted-foreground">
            Health of the monitoring server and of the profiles it runs.
          </p>
        </div>
        <StatusBadge
          label={healthLabel}
          tone={healthTone}
          icon={HEALTH_ICONS[health.status] ?? <TriangleAlert className={BADGE_ICON_CLASSES} />}
        />
      </header>

      {isEngaged ? (
        <p
          role="status"
          aria-live="polite"
          className="mt-lg flex flex-wrap items-center gap-sm rounded-button border border-warn/40 bg-warn/10 px-lg py-md text-sm text-foreground"
        >
          <ShieldAlert aria-hidden="true" className="size-4 shrink-0 text-warn" />
          <span className="font-medium">Degraded · kill switch engaged</span>
          <span className="min-w-0 break-words text-muted-foreground">
            {reason === '' ? EMPTY_PLACEHOLDER : reason}
          </span>
        </p>
      ) : null}

      <div className="mt-lg grid gap-md sm:grid-cols-2 xl:grid-cols-4">
        <StatTile label="Version" value={textOrPlaceholder(health.version)} icon={<Tag className="size-4" />} />
        <StatTile
          label="Uptime"
          value={formatDuration(health.uptime_seconds)}
          icon={<Timer className="size-4" />}
        />
        <StatTile
          label="Profiles running"
          value={`${formatInteger(health.profiles_running)} / ${formatInteger(health.profiles_total)}`}
          hint={`${formatInteger(profiles.length)} listed here`}
          icon={<Layers className="size-4" />}
        />
        <div className="rounded-card border border-border bg-card p-lg shadow-sm">
          <div className="flex items-start justify-between gap-sm">
            <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Kill switch
            </span>
            <span aria-hidden="true" className="inline-flex shrink-0 text-muted-foreground">
              {isEngaged ? <ShieldAlert className="size-4" /> : <ShieldCheck className="size-4" />}
            </span>
          </div>
          <div className="mt-sm">
            <StatusBadge
              label={isEngaged ? 'Engaged' : 'Released'}
              tone={isEngaged ? 'error' : 'ok'}
              icon={
                isEngaged ? (
                  <ShieldAlert className={BADGE_ICON_CLASSES} />
                ) : (
                  <ShieldCheck className={BADGE_ICON_CLASSES} />
                )
              }
            />
          </div>
          <dl className="mt-sm grid gap-xs text-xs">
            <div className="flex flex-wrap items-baseline gap-xs">
              <dt className="text-muted-foreground">Reason</dt>
              <dd className="min-w-0 break-words font-mono text-foreground">
                {reason === '' ? EMPTY_PLACEHOLDER : reason}
              </dd>
            </div>
            <div className="flex flex-wrap items-baseline gap-xs">
              <dt className="text-muted-foreground">Changed at</dt>
              <dd className="font-mono tabular-nums text-foreground">
                <time dateTime={killSwitch.changed_at ?? undefined}>
                  {formatTimestamp(killSwitch.changed_at)}
                </time>
              </dd>
            </div>
          </dl>
        </div>
      </div>
    </section>
  );
}
