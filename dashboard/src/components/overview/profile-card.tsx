import type { ReactNode } from 'react';

import {
  Ban,
  Banknote,
  CircleCheck,
  CirclePause,
  CircleX,
  FlaskConical,
  LoaderCircle,
  Minus,
  TrendingDown,
  TrendingUp,
  TriangleAlert,
} from 'lucide-react';
import Link from 'next/link';

import { StatusBadge, profileStatusTone, type StatusTone } from '@/components/ui/status-badge';
import {
  EMPTY_PLACEHOLDER,
  formatDuration,
  formatInteger,
  formatMoney,
  formatRatioAsPercent,
  formatTimestamp,
  isFiniteNumber,
  trendLabel,
  trendOf,
} from '@/lib/format';
import type { ProfileSnapshot, ProfileStatus, RunMode } from '@/lib/types';

/** Props of {@link ProfileCard}. */
export interface ProfileCardProps {
  /** One profile as `GET /api/profiles` emits it. */
  profile: ProfileSnapshot;
}

const BADGE_ICON_CLASSES = 'size-3.5';

const MODE_BADGES: Record<RunMode, { label: string; tone: StatusTone; icon: ReactNode }> = {
  paper: {
    label: 'Paper',
    tone: 'info',
    icon: <FlaskConical className={BADGE_ICON_CLASSES} />,
  },
  live: {
    label: 'Live',
    tone: 'warn',
    icon: <Banknote className={BADGE_ICON_CLASSES} />,
  },
};

const STATUS_BADGES: Record<ProfileStatus, { label: string; icon: ReactNode; tone: StatusTone }> = {
  starting: { label: 'Starting', tone: 'info', icon: <LoaderCircle className={BADGE_ICON_CLASSES} /> },
  running: { label: 'Running', tone: 'ok', icon: <CircleCheck className={BADGE_ICON_CLASSES} /> },
  degraded: {
    label: 'Degraded',
    tone: 'warn',
    icon: <TriangleAlert className={BADGE_ICON_CLASSES} />,
  },
  halted: { label: 'Halted', tone: 'error', icon: <CirclePause className={BADGE_ICON_CLASSES} /> },
  stopped: { label: 'Stopped', tone: 'neutral', icon: <Ban className={BADGE_ICON_CLASSES} /> },
  error: { label: 'Error', tone: 'error', icon: <CircleX className={BADGE_ICON_CLASSES} /> },
};

const TREND_TEXT_CLASSES: Record<'up' | 'down' | 'flat', string> = {
  up: 'text-profit',
  down: 'text-loss',
  flat: 'text-muted-foreground',
};

const TREND_ICONS: Record<'up' | 'down' | 'flat', ReactNode> = {
  up: <TrendingUp className={BADGE_ICON_CLASSES} />,
  down: <TrendingDown className={BADGE_ICON_CLASSES} />,
  flat: <Minus className={BADGE_ICON_CLASSES} />,
};

/** Render `value`, or the em dash placeholder when it is absent or blank. */
function textOrPlaceholder(value: string | null | undefined): string {
  const trimmed = (value ?? '').trim();
  return trimmed === '' ? EMPTY_PLACEHOLDER : trimmed;
}

/** Deterministic, hydration-safe DOM id of the card heading. */
function headingId(profileId: string): string {
  return `profile-${profileId.replace(/[^a-zA-Z0-9_-]+/g, '-')}`;
}

interface FieldProps {
  /** Visible label of the value (never optional: every value is labelled). */
  label: string;
  /** Already formatted value; blank or absent renders an em dash. */
  value?: string;
  /** Rich value (a trend indicator, a link, ...); wins over `value`. */
  children?: ReactNode;
}

/** One labelled value of the card's definition grid. */
function Field({ label, value, children }: FieldProps) {
  const text = value ?? EMPTY_PLACEHOLDER;
  return (
    <div className="rounded-button border border-border/60 bg-muted/30 px-lg py-md">
      <dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="mt-xs font-mono text-sm tabular-nums text-foreground">
        {children ?? (text.trim() === '' ? EMPTY_PLACEHOLDER : text)}
      </dd>
    </div>
  );
}

/**
 * One profile of the live overview.
 *
 * The card is labelled by its heading, whose link goes to the per-profile detail
 * route; the rest of the card stays free of nested interactive content, so a
 * keyboard user never lands on a link wrapping a button. Every value carries an
 * explicit label, every state pairs its tone with a text label and an icon, and
 * every absent value renders the em dash placeholder — never `NaN`, `undefined`
 * or an empty cell.
 */
export function ProfileCard({ profile }: ProfileCardProps) {
  const mode = MODE_BADGES[profile.mode] ?? {
    label: textOrPlaceholder(profile.mode),
    tone: 'neutral' as StatusTone,
    icon: <FlaskConical className={BADGE_ICON_CLASSES} />,
  };
  const status = STATUS_BADGES[profile.status] ?? {
    label: textOrPlaceholder(profile.status),
    tone: profileStatusTone(profile.status),
    icon: <CircleX className={BADGE_ICON_CLASSES} />,
  };
  const hasReturn = isFiniteNumber(profile.total_return);
  const trend = trendOf(profile.total_return);
  const lastError = profile.health.last_error;

  return (
    <article
      aria-labelledby={headingId(profile.profile_id)}
      className="flex flex-col rounded-card border border-border bg-card p-xl text-card-foreground shadow-md"
    >
      <header className="flex flex-wrap items-start justify-between gap-md">
        <div className="min-w-0">
          <h3
            id={headingId(profile.profile_id)}
            className="font-mono text-base font-semibold text-card-foreground"
          >
            <Link
              href={`/profiles/${encodeURIComponent(profile.profile_id)}`}
              className="rounded-button underline-offset-4 hover:text-accent hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background motion-safe:transition-colors motion-safe:duration-200"
            >
              {textOrPlaceholder(profile.profile_id)}
            </Link>
          </h3>
        </div>
        <div className="flex flex-wrap items-center gap-sm">
          <StatusBadge label={mode.label} tone={mode.tone} icon={mode.icon} />
          <StatusBadge label={status.label} tone={status.tone} icon={status.icon} />
        </div>
      </header>

      <dl className="mt-lg grid gap-md sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        <Field label="Profile id" value={textOrPlaceholder(profile.profile_id)} />
        <Field label="Symbol" value={textOrPlaceholder(profile.symbol)} />
        <Field label="Timeframe" value={textOrPlaceholder(profile.timeframe)} />
        <Field label="Strategy" value={textOrPlaceholder(profile.strategy)} />
        <Field label="Equity" value={formatMoney(profile.equity)} />
        <Field label="Cash" value={formatMoney(profile.cash)} />
        <Field label="Position value" value={formatMoney(profile.position_value)} />
        <Field label="Initial balance" value={formatMoney(profile.initial_balance)} />
        <Field label="Total return" value={formatRatioAsPercent(profile.total_return, { signed: true })}>
          {hasReturn ? (
            <span className="inline-flex flex-wrap items-center gap-xs">
              <span aria-hidden="true" className={TREND_TEXT_CLASSES[trend]}>
                {TREND_ICONS[trend]}
              </span>
              <span className={TREND_TEXT_CLASSES[trend]}>
                {formatRatioAsPercent(profile.total_return, { signed: true })}
              </span>
              <span className="text-muted-foreground">{trendLabel(profile.total_return)}</span>
            </span>
          ) : undefined}
        </Field>
        <Field label="Trades" value={formatInteger(profile.n_trades)} />
        <Field label="Open positions" value={formatInteger(profile.open_positions)} />
        <Field label="Last candle" value={formatTimestamp(profile.health.last_candle_at)} />
        <Field label="Candle lag" value={formatDuration(profile.health.lag_seconds)} />
        <Field label="Started at" value={formatTimestamp(profile.started_at)} />
        <Field label="Updated at" value={formatTimestamp(profile.updated_at)} />
      </dl>

      {lastError !== null ? (
        <p
          role="status"
          className="mt-lg flex flex-wrap items-center gap-sm rounded-button border border-warn/40 bg-warn/10 px-lg py-md text-sm text-foreground"
        >
          <TriangleAlert aria-hidden="true" className="size-4 shrink-0 text-warn" />
          <span className="font-medium">Last error</span>
          <span className="min-w-0 break-words">{textOrPlaceholder(lastError)}</span>
        </p>
      ) : null}
    </article>
  );
}
